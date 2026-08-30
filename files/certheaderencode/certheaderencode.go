// Package certheaderencode percent-encodes the header that carries the client
// certificate.
//
// Traefik's passTLSClientCert middleware writes the certificate as plain
// base64, with the PEM markers and newlines removed and nothing escaped. ILM
// core URL-decodes that header before it decodes the base64, because it
// expects the value ingress-nginx produces with $ssl_client_escaped_cert. The
// decoding turns every '+' of the base64 into a space and the certificate is
// rejected with "Illegal base64 character 20". Encoding the value here gives
// core exactly what its decoding step expects.
package certheaderencode

import (
	"context"
	"net/http"
	"net/url"
)

// DefaultHeader is the header passTLSClientCert writes the certificate to.
const DefaultHeader = "X-Forwarded-Tls-Client-Cert"

// Config holds the plugin configuration.
type Config struct {
	// Header to encode. Defaults to DefaultHeader.
	Header string `json:"header,omitempty"`
}

// CreateConfig creates the default plugin configuration.
func CreateConfig() *Config {
	return &Config{Header: DefaultHeader}
}

type encoder struct {
	next   http.Handler
	name   string
	header string
}

// New creates a new certheaderencode middleware.
func New(_ context.Context, next http.Handler, config *Config, name string) (http.Handler, error) {
	header := config.Header
	if header == "" {
		header = DefaultHeader
	}

	return &encoder{next: next, name: name, header: header}, nil
}

func (e *encoder) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	if value := req.Header.Get(e.header); value != "" {
		req.Header.Set(e.header, url.QueryEscape(value))
	}

	e.next.ServeHTTP(rw, req)
}
