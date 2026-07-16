// tls_proxy.go – TLS fingerprint rotation proxy (works with utls v1.4.x+)
// Build:
//   go build -o tls_proxy tls_proxy.go

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
	"strings"
	"time"

	utls "github.com/refraction-networking/utls"
)

var listenAddr = flag.String("listen", "127.0.0.1:8080", "Address to listen on")

// Fingerprint map using constants that exist in utls v1.4.x (no Chrome_108 etc.)
var fingerprintMap = map[string]utls.ClientHelloID{
	// Chrome mappings -> HelloChrome_106 (the newest available in old utls)
	"chrome_108":         utls.HelloChrome_106,
	"chrome_109":         utls.HelloChrome_106,
	"chrome_110":         utls.HelloChrome_106,
	"chrome_111":         utls.HelloChrome_106,
	"chrome_112":         utls.HelloChrome_106,
	"chrome_114":         utls.HelloChrome_106,
	"chrome_120":         utls.HelloChrome_106,
	"chrome_123":         utls.HelloChrome_106,
	"chrome_112_windows": utls.HelloChrome_106,
	"chrome_112_mac":     utls.HelloChrome_106,

	// Firefox mappings -> HelloFirefox_105 (newest Firefox in old utls)
	"firefox_115": utls.HelloFirefox_105,
	"firefox_116": utls.HelloFirefox_105,
	"firefox_117": utls.HelloFirefox_105,
	"firefox_118": utls.HelloFirefox_105,
	"firefox_119": utls.HelloFirefox_105,
	"firefox_121": utls.HelloFirefox_105,

	// Safari mappings -> HelloSafari_14
	"safari_15_5": utls.HelloSafari_14,
	"safari_16_0": utls.HelloSafari_14,
	"safari_16_1": utls.HelloSafari_14,
	"safari_17_0": utls.HelloSafari_14,
}

func parseBasicAuth(authHeader string) (username, password string) {
	const prefix = "Basic "
	if !strings.HasPrefix(authHeader, prefix) {
		return
	}
	payload, _ := base64.StdEncoding.DecodeString(authHeader[len(prefix):])
	pair := strings.SplitN(string(payload), ":", 2)
	if len(pair) != 2 {
		return
	}
	decoded, err := url.PathUnescape(pair[0])
	if err != nil {
		decoded = pair[0]
	}
	return decoded, pair[1]
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
	fpID := "chrome_120"  // default
	var upstreamURL string
	if auth := r.Header.Get("Proxy-Authorization"); auth != "" {
		upstreamURL, fpID = parseBasicAuth(auth)
	}

	helloID, ok := fingerprintMap[fpID]
	if !ok {
		helloID = utls.HelloChrome_106
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

	tlsConn := utls.UClient(targetConn, &utls.Config{
		ServerName: strings.Split(r.URL.Host, ":")[0],
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

	go io.Copy(tlsConn, clientConn)
	io.Copy(clientConn, tlsConn)
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
