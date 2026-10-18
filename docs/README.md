# Documentation

Technical stakeholders and deployment operators: begin with the
[application and implementation guide](application-guide.md). It explains
API and Python data flow, snapshot creation, Docker packaging, deployment
architectures, cloud setup, local prerequisites, Asana token provisioning and
CLI use on Linux, Windows and macOS, plus the optional web dashboard.

Engineers: continue with [deployment design](deployment.md), then the
[Docker](docker.md), [Postgres](postgres.md) and [Google Sheets](sheets.md)
runbooks. [Validation evidence](../VALIDATION.md) distinguishes offline tests,
real database tests, authorized live checks and remaining production work.

The application guide has a generated PDF for sharing. Rebuild it locally with
Python and ReportLab installed in a separate documentation environment:

```sh
python3 tools/build_guide_pdf.py
```

This reads the tracked Markdown guide and writes
`output/pdf/asana-reporting-guide.pdf`. The generated output is ignored by Git
and excluded from Docker builds. Diagrams use the same Mermaid node labels and
connections as the Markdown guide; CLI fences preserve line breaks. Review the
rendered PDF when editing the guide.
The collector and other application runtimes do not require ReportLab.
