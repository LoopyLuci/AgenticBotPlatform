{
  description = "Bot Server desktop shell — Nix dev shell and package for NixOS/Nix users";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ rust-overlay.overlays.default ];
        };

        rustToolchain = pkgs.rust-bin.stable.latest.default;

        # Same libraries the README lists for Debian/Fedora/Arch — Tauri's
        # window (WebKitGTK), tray (AppIndicator), and icon (librsvg) deps.
        tauriRuntimeDeps = with pkgs; [
          webkitgtk_4_1
          gtk3
          librsvg
          libayatana-appindicator
          openssl
          glib-networking
        ];

        tauriBuildDeps = with pkgs; [
          pkg-config
          patchelf
          wrapGAppsHook3
        ];
      in
      {
        # `nix develop` — a shell with every Linux prerequisite the README's
        # Debian/Fedora/Arch sections install manually, so `cargo tauri build`
        # / `cargo tauri dev` work the same way on NixOS without editing
        # /etc or touching a global profile.
        #
        # This does NOT solve the separate "pip-installed compiled wheel
        # can't find its shared libraries" problem for bot/'s Python venv
        # (cryptography, etc. ship prebuilt manylinux wheels that assume FHS
        # paths NixOS doesn't have) — see the NixOS section in README.md for
        # the two ways to work around that (nix-ld, or a buildFHSEnv shell)
        # before running `./scripts/run.sh`.
        devShells.default = pkgs.mkShell {
          buildInputs = tauriRuntimeDeps;
          nativeBuildInputs = tauriBuildDeps ++ [
            rustToolchain
            pkgs.python311
            pkgs.cargo-tauri
          ];

          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath tauriRuntimeDeps;

          shellHook = ''
            echo "Bot Server Nix dev shell — Rust $(rustc --version), $(python3 --version)"
            echo "Next: ./scripts/run.sh to set up the venv, then cd desktop-app/src-tauri && cargo tauri dev"
          '';
        };

        # `nix build` — packages just the Tauri/Rust desktop shell binary as
        # a Nix derivation (the idiomatic Nix unit), NOT the Windows-style
        # self-contained bundle with a Python venv baked in (bundling a
        # pip-installed venv into an immutable /nix/store path fights Nix's
        # model — see devShell note above). Run the resulting `bot-server`
        # binary from the repo root, alongside a `.venv` set up via
        # `./scripts/run.sh`, same as the "Development" build in README.md,
        # not the standalone Windows-style production bundle.
        packages.default = pkgs.rustPlatform.buildRustPackage {
          pname = "bot-server";
          version = "0.1.1";
          src = ./desktop-app/src-tauri;
          cargoLock.lockFile = ./desktop-app/src-tauri/Cargo.lock;

          nativeBuildInputs = tauriBuildDeps ++ [ rustToolchain pkgs.python311 ];
          buildInputs = tauriRuntimeDeps;

          # The Tauri bundler (.deb/.rpm/AppImage) assumes an FHS target and
          # a venv sitting next to the binary at build time — neither fits a
          # Nix derivation. Build the plain binary instead; NixOS users run
          # it via the devShell + repo checkout, not as a bundled installer.
          buildPhase = ''
            runHook preBuild
            cargo build --release --offline
            runHook postBuild
          '';

          installPhase = ''
            runHook preInstall
            mkdir -p $out/bin
            cp target/release/bot-server $out/bin/
            runHook postInstall
          '';

          meta = with pkgs.lib; {
            description = "All-in-one desktop shell for Bot Server";
            homepage = "https://github.com/LoopyLuci/BotServer";
            # No LICENSE file exists in the repo yet — add one and set this
            # field once it does.
            platforms = platforms.linux;
          };
        };
      });
}
