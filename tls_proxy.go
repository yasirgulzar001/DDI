// tls_proxy.go – TLS fingerprint rotation proxy using refraction-networking/utls
// Build:
//   go mod init tlsproxy
//   go get github.com/refraction-networking/utls@latest
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

var (
	listenAddr = flag.String("listen", "127.0.0.1:8080", "Address to listen on")
	ja3TestURL = flag.String("ja3-test", "https://ja3er.com/json", "JA3 test URL")
)

// All fingerprints are guaranteed to exist in utls v1.6.7+
var fingerprintMap = map[string]utls.ClientHelloID{
	"chrome_108":         utls.HelloChrome_108,
	"chrome_109":         utls.HelloChrome_109,
	"chrome_110":         utls.HelloChrome_110,
	"chrome_111":         utls.HelloChrome_111,
	"chrome_112":         utls.HelloChrome_112,
	"chrome_114":         utls.HelloChrome_114,
	"chrome_116":         utls.HelloChrome_116,
	"chrome_117":         utls.HelloChrome_117,
	"chrome_120":         utls.HelloChrome_120,
	"chrome_123":         utls.HelloChrome_123,
	"chrome_124":         utls.HelloChrome_124,
	"firefox_115":        utls.HelloFirefox_115,
	"firefox_116":        utls.HelloFirefox_116,
	"firefox_117":        utls.HelloFirefox_117,
	"firefox_118":        utls.HelloFirefox_118,
	"firefox_119":        utls.HelloFirefox_119,
	"firefox_121":        utls.HelloFirefox_121,
	"firefox_127":        utls.HelloFirefox_127,
	"safari_15_5":        utls.HelloSafari_15_5,
	"safari_15_6":        utls.HelloSafari_15_6,
	"safari_16_0":        utls.HelloSafari_16_0,
	"safari_16_1":        utls.HelloSafari_16_1,
	"safari_17_0":        utls.HelloSafari_17_0,
	"safari_18_0":        utls.HelloSafari_18_0,
	"ios_14":             utls.HelloIOS_14,
	"ios_15":             utls.HelloIOS_15,
	"ios_16":             utls.HelloIOS_16,
	"android_11":         utls.HelloAndroid_11,
	"edge_106":           utls.HelloEdge_106,
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
	// Use PathUnescape for URL‑encoded upstream, not QueryUnescape (which mangles '+')
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
	fpID := "chrome_120"
	var upstreamURL string
	if auth := r.Header.Get("Proxy-Authorization"); auth != "" {
		upstreamURL, fpID = parseBasicAuth(auth)
	}

	helloID, ok := fingerprintMap[fpID]
	if !ok {
		helloID = utls.HelloChrome_120
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
