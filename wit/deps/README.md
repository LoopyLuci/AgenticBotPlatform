# `wit/deps/` - the WASI p2 interfaces, vendored

These are the upstream WASI 0.2.12 WIT documents, unmodified, from the `wasip2` crate
(`wit/deps/*.wit`). They are here because WIT resolution needs them next to the world that
imports them, and because three places - the host runtime, the guest SDK and every example -
should be compiling against one copy of an interface rather than three.

* Upstream: <https://github.com/WebAssembly/WASI>, published as the `wasip2` crate
* Licence: Apache-2.0 WITH LLVM-exception (the same licence the WASI specification itself is
  under, and the same one `wasmtime-wasi` uses)

To move to a newer WASI: copy the files from the matching `wasip2` release, then bump the
`@0.2.12` versions in [`../abp-plugin.wit`](../abp-plugin.wit). The host pins
`wasmtime-wasi`'s version to the one that ships these interfaces, so the two move together.
