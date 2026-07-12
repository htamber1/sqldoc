// Package main is the entry-point skeleton for terraform-provider-sqldoc.
//
// STATUS: stub. This sketches the shape of a native Terraform provider that
// exposes sqldoc's documentation and PII/compliance scanning as data sources so
// database documentation becomes a first-class part of an infrastructure-as-code
// plan/apply lifecycle. The data-source Read implementations shell out to the
// `sqldoc` CLI (which must be on PATH); a future version may bind directly.
//
// Until this is fleshed out and published to the Terraform Registry, use the
// null_resource + local-exec pattern documented in README.md (Option A), which
// requires no custom provider.
//
// Build (once the framework deps are vendored):
//
//	go build -o terraform-provider-sqldoc
package main

import (
	"context"
	"flag"
	"log"
)

const providerAddress = "registry.terraform.io/htamber1/sqldoc"

// Provider configuration mirrors the sqldoc CLI connection surface.
type providerConfig struct {
	ConnectionString string
	Dialect          string // sqlserver, azuresql, postgres, mysql, ...
	Mode             string // local (Ollama) or cloud (Anthropic)
	Model            string
}

// dataSourceDocumentation ~ data "sqldoc_documentation": runs `sqldoc doc`.
//
//	Attributes: output (path), format; computed: table_count, generated_at.
//
// dataSourcePIIScan ~ data "sqldoc_pii_scan": runs `sqldoc scan`.
//
//	Attributes: fail_on; computed: high_risk_count, medium_risk_count,
//	tables_affected.
//
// Each Read would invoke the CLI with the provider's connection settings and
// parse its --json output into the data source's computed attributes.

func main() {
	var debug bool
	flag.BoolVar(&debug, "debug", false, "run the provider with support for debuggers")
	flag.Parse()

	_ = context.Background()
	_ = providerConfig{}

	// TODO: serve the provider via the Terraform Plugin Framework, e.g.
	//   providerserver.Serve(ctx, New(), providerserver.ServeOpts{
	//       Address: providerAddress,
	//       Debug:   debug,
	//   })
	log.Printf("terraform-provider-sqldoc is a stub (%s). "+
		"Use the null_resource + local-exec pattern in README.md for now.",
		providerAddress)
}
