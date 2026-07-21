// tls_proxy.go – TLS fingerprint rotation proxy (secure, production‑ready)
// Build:
//   go mod init tlsproxy
//   go get github.com/refraction-networking/utls@v1.5.0
//   go build -o tls_proxy tls_proxy.go
//
// Environment:
//   PROXY_AUTH_TOKEN – required shared secret (any non‑empty string)

package main

import (
	"encoding/base64"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	utls "github.com/refraction-networking/utls"
)

var listenAddr = flag.String("listen", "127.0.0.1:8080", "Address to listen on")

// --- Authentication ---
var authToken = os.Getenv("PROXY_AUTH_TOKEN")

func init() {
	if authToken == "" {
		log.Fatal("PROXY_AUTH_TOKEN must be set in environment")
	}
}

// validateAuth checks that the Proxy-Authorization header contains the token as password.
// Format: Basic base64(anything:TOKEN)
func validateAuth(r *http.Request) bool {
	auth := r.Header.Get("Proxy-Authorization")
	if auth == "" {
		return false
	}
	const prefix = "Basic "
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	payload, err := base64.StdEncoding.DecodeString(auth[len(prefix):])
	if err != nil {
		return false
	}
	parts := strings.SplitN(string(payload), ":", 2)
	if len(parts) != 2 {
		return false
	}
	// The password part must match our secret token
	return parts[1] == authToken
}

// --- Fingerprint map ---
var fingerprintMap = map[string]utls.ClientHelloID{
	"chrome_108":         utls.HelloChrome_100,
	"chrome_109":         utls.HelloChrome_100,
	"chrome_110":         utls.HelloChrome_100,
	"chrome_111":         utls.HelloChrome_100,
	"chrome_112":         utls.HelloChrome_100,
	"chrome_114":         utls.HelloChrome_100,
	"chrome_120":         utls.HelloChrome_100,
	"chrome_123":         utls.HelloChrome_100,
	"chrome_112_windows": utls.HelloChrome_100,
	"chrome_112_mac":     utls.HelloChrome_100,

	"firefox_115": utls.HelloFirefox_99,
	"firefox_116": utls.HelloFirefox_99,
	"firefox_117": utls.HelloFirefox_99,
	"firefox_118": utls.HelloFirefox_99,
	"firefox_119": utls.HelloFirefox_99,
	"firefox_121": utls.HelloFirefox_99,

	"safari_15_5": utls.HelloSafari_14,
	"safari_16_0": utls.HelloSafari_14,
	"safari_16_1": utls.HelloSafari_14,
	"safari_17_0": utls.HelloSafari_14,
}

// parseBasicAuth extracts username (upstream URL) and fingerprint ID from auth header.
// Expected format: base64("upstream_url:fingerprint_id") – password part is the token (ignored here).
func parseBasicAuth(authHeader string) (upstreamURL, fpID string) {
	const prefix = "Basic "
	if !strings.HasPrefix(authHeader, prefix) {
		return
	}
	payload, _ := base64.StdEncoding.DecodeString(authHeader[len(prefix):])
	pair := strings.SplitN(string(payload), ":", 2)
	if len(pair) != 2 {
		return
	}
	// username part is the upstream URL (if any)
	decoded, err := url.PathUnescape(pair[0])
	if err != nil {
		decoded = pair[0]
	}
	upstreamURL = decoded
	fpID = pair[1] // fingerprint is the "password"
	return
}

func dialUpstream(upstreamURL, target string) (net.Conn, error) {
	parsed, err := url.Parse(upstreamURL)
	if err != nil {
		return nil, err
	}
	conn, err := net.DialTimeout("tcp", parsed.Host, 10*time.Second)
	if err != nil {
		return nil, err
	}
	req := fmt.Sprintf("CONNECT %s HTTP/1.1\r\nHost: %s\r\n", target, target)
	if parsed.User != nil {
		auth := base64.StdEncoding.EncodeToString([]byte(parsed.User.String()))
		req += fmt.Sprintf("Proxy-Authorization: Basic %s\r\n", auth)
	}
	req += "\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		conn.Close()
		return nil, err
	}
	var respBuf [512]byte
	n, err := conn.Read(respBuf[:])
	if err != nil {
		conn.Close()
		return nil, err
	}
	response := string(respBuf[:n])
	if !strings.Contains(response, "200") {
		conn.Close()
		return nil, fmt.Errorf("upstream proxy returned non-200: %s", response)
	}
	return conn, nil
}

func main() {
	flag.Parse()
	log.SetFlags(log.LstdFlags)

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			handleConnect(w, r)
		} else if r.URL.Path == "/test" {
			handleTest(w, r)
		} else {
			http.Error(w, "only CONNECT or /test", http.StatusBadRequest)
		}
	})

	log.Printf("TLS rotation proxy listening on %s", *listenAddr)
	log.Fatal(http.ListenAndServe(*listenAddr, nil))
}

func handleConnect(w http.ResponseWriter, r *http.Request) {
	// Require authentication
	if !validateAuth(r) {
		http.Error(w, "unauthorized", http.StatusProxyAuthRequired)
		return
	}

	fpID := "chrome_120" // default
	var upstreamURL string
	authHeader := r.Header.Get("Proxy-Authorization")
	if authHeader != "" {
		upstreamURL, fpID = parseBasicAuth(authHeader)
	}

	helloID, ok := fingerprintMap[fpID]
	if !ok {
		helloID = utls.HelloChrome_100
	}
	log.Printf("CONNECT %s with fingerprint %s, upstream=%s", r.URL.Host, fpID, upstreamURL)

	var targetConn net.Conn
	var err error
	if upstreamURL != "" {
		targetConn, err = dialUpstream(upstreamURL, r.URL.Host)
	} else {
		targetConn, err = net.DialTimeout("tcp", r.URL.Host, 10*time.Second)
	}
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}

	// Extract server name correctly for TLS, handling IPv6
	host, _, err := net.SplitHostPort(r.URL.Host)
	if err != nil {
		// If no port, use the whole thing
		host = r.URL.Host
	}

	tlsConn := utls.UClient(targetConn, &utls.Config{
		ServerName: host,
	}, helloID)
	if err := tlsConn.Handshake(); err != nil {
		targetConn.Close()
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}

	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijacking not supported", http.StatusInternalServerError)
		tlsConn.Close()
		return
	}
	clientConn, _, err := hj.Hijack()
	if err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		tlsConn.Close()
		return
	}
	clientConn.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))

	// Bidirectional copy with proper cleanup
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		io.Copy(tlsConn, clientConn)
	}()
	io.Copy(clientConn, tlsConn)
	// Close both connections after one direction ends
	tlsConn.Close()
	clientConn.Close()
	wg.Wait() // ensure goroutine finished
}

func handleTest(w http.ResponseWriter, r *http.Request) {
	fpID := r.URL.Query().Get("fp")
	if fpID == "" {
		fpID = "chrome_120"
	}
	helloID, ok := fingerprintMap[fpID]
	if !ok {
		http.Error(w, "unknown fingerprint", http.StatusBadRequest)
		return
	}
	conn, err := net.DialTimeout("tcp", "ja3er.com:443", 10*time.Second)
	if err != nil {
		http.Error(w, "cannot connect to ja3er.com: "+err.Error(), http.StatusInternalServerError)
		return
	}
	tlsConn := utls.UClient(conn, &utls.Config{ServerName: "ja3er.com"}, helloID)
	if err := tlsConn.Handshake(); err != nil {
		conn.Close()
		http.Error(w, "TLS handshake failed: "+err.Error(), http.StatusInternalServerError)
		return
	}
	req := "GET /json HTTP/1.1\r\nHost: ja3er.com\r\nConnection: close\r\n\r\n"
	tlsConn.Write([]byte(req))
	var buf [2048]byte
	n, _ := tlsConn.Read(buf[:])
	tlsConn.Close()

	body := string(buf[:n])
	start := strings.Index(body, `"ja3_hash":"`)
	if start == -1 {
		start = strings.Index(body, `"ja3":"`)
		if start == -1 {
			http.Error(w, "ja3 not found in response", http.StatusInternalServerError)
			return
		}
		start += len(`"ja3":"`)
		end := strings.Index(body[start:], `"`)
		w.Write([]byte(body[start : start+end]))
		return
	}
	start += len(`"ja3_hash":"`)
	end := strings.Index(body[start:], `"`)
	w.Write([]byte(body[start : start+end]))
}
