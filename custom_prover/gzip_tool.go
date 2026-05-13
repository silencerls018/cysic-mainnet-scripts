package main

import (
	"compress/gzip"
	"encoding/base64"
	"fmt"
	"io"
	"os"
)

// gzip_tool: Compress file with Go's gzip (level -1) and output base64
// Build: go build -o gzip_tool gzip_tool.go
// Usage: gzip_tool <file_path>

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: gzip_tool <file>")
		os.Exit(1)
	}
	data, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	pr, pw := io.Pipe()
	go func() {
		w, _ := gzip.NewWriterLevel(pw, -1)
		w.Write(data)
		w.Close()
		pw.Close()
	}()
	compressed, _ := io.ReadAll(pr)
	fmt.Print(base64.StdEncoding.EncodeToString(compressed))
}
