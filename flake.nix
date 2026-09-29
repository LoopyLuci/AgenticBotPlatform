{
  description = "Agentic Bot Platform (ABP): the package, a NixOS service module, a VM test, the desktop shell and a dev shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay }:
    let
      version = builtins.head (builtins.match ''.*__version__ = "([^"]+)".*'' (builtins.readFile ./bot/__init__.py));
    in
    {
      # services.agentic-bot-platform (nix/module.nix). Add to a system:
      #   imports = [ agentic-bot-platform.nixosModules.default ];
      #   services.agentic-bot-platform.enable = true;
      nixosModules.default = import ./nix/module.nix self;
      nixosModules.agentic-bot-platform = self.nixosModules.default;

      overlays.default = final: prev: {
        agentic-bot-platform = final.callPackage ./nix/package.nix { inherit version; };
      };
    }
    // flake-utils.lib.eachDefaultSystem (system:
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

        abp = pkgs.callPackage ./nix/package.nix { inherit version; };
      in
      {
        # `nix build` / `nix run`: ABP itself (abp-server, abp, abp-tui). State goes to ABP_HOME, never the store.
        packages.default = abp;
        packages.agentic-bot-platform = abp;

        # The Tauri desktop shell (a window around the dashboard). It starts the server from `abp-server` on PATH
        # when it is installed next to packages.default.
        packages.desktop = pkgs.rustPlatform.buildRustPackage {
          pname = "agentic-bot-platform-desktop";
          inherit version;
          src = ./desktop-app/src-tauri;
          cargoLock.lockFile = ./desktop-app/src-tauri/Cargo.lock;

          nativeBuildInputs = tauriBuildDeps ++ [ rustToolchain pkgs.python311 ];
          buildInputs = tauriRuntimeDeps;

          # The Tauri bundler (.deb/.rpm/AppImage) assumes an FHS target; build the plain binary instead.
          buildPhase = ''
            runHook preBuild
            cargo build --release --offline
            runHook postBuild
          '';

          installPhase = ''
            runHook preInstall
            mkdir -p $out/bin
            cp target/release/agentic-bot-platform $out/bin/
            runHook postInstall
          '';

          meta = with pkgs.lib; {
            description = "All-in-one desktop shell for Agentic Bot Platform";
            homepage = "https://github.com/LoopyLuci/AgenticBotPlatform";
            license = licenses.mit;
            platforms = platforms.linux;
          };
        };

        apps.default = { type = "app"; program = "${abp}/bin/abp-server"; };
        apps.cli = { type = "app"; program = "${abp}/bin/abp"; };
        apps.tui = { type = "app"; program = "${abp}/bin/abp-tui"; };

        # `nix develop` — a shell with every Linux prerequisite the README's
        # Debian/Fedora/Arch sections install manually, so `cargo tauri build`
        # / `cargo tauri dev` work the same way on NixOS without editing
        # /etc or touching a global profile. `abp`, `abp-server` and `abp-tui`
        # from the package are on PATH too.
        devShells.default = pkgs.mkShell {
          buildInputs = tauriRuntimeDeps;
          nativeBuildInputs = tauriBuildDeps ++ [
            rustToolchain
            abp.pythonEnv
            pkgs.cargo-tauri
          ];

          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath tauriRuntimeDeps;

          shellHook = ''
            echo "Agentic Bot Platform Nix dev shell — Rust $(rustc --version), $(python3 --version) with ABP's dependencies"
            echo "Run the server from the checkout: python -m bot.sentinel.guardian (state in ABP_HOME if set)"
          '';
        };
      }
      // nixpkgs.lib.optionalAttrs (system == "x86_64-linux" || system == "aarch64-linux") {
        # `nix flake check`: the NixOS module in a VM (needs KVM).
        checks.nixos = import ./nix/test.nix { inherit self pkgs; };
        checks.package = abp;
      });
}
