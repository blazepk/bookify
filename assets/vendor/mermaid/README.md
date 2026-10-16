# Vendored Mermaid

bookify bundles this build so Mermaid diagrams render offline and over `file://`.

- Package: mermaid 11.17.2 (MIT, see `LICENSE`)
- Source: https://cdn.jsdelivr.net/npm/mermaid@11.17.2/dist/mermaid.min.js
  (identical to `package/dist/mermaid.min.js` in https://registry.npmjs.org/mermaid/-/mermaid-11.17.2.tgz,
  npm integrity sha512-V6K3C8EBdEsPFZXSKMJe6ppQOENxuHARr9GvHX4hh47lAbhMRD9qf4oEK7LoaRQxULMa80/qt5gHO73aCleBBg==)
- SHA-256 of `mermaid.min.js`: 581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8
- Third-party notices of the libraries inside the bundle are kept as comments at the end of the file.

To upgrade, replace both files, update this note, and rerun `tests/test_bookify.py`.
