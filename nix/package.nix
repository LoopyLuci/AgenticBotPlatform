# ABP (the server, the CLI and the TUI) as a Nix package.
#
# The code goes into the store read-only and every dependency comes from nixpkgs, so no pip, no venv and no
# prebuilt wheel that expects FHS paths. State never lives next to the code: the wrappers set ABP_HOME (default
# $XDG_DATA_HOME/agentic-bot-platform, or /var/lib/abp under the NixOS module), and ABP keeps its .env, config,
# data and logs there (docs/embedding.md).
#
#   abp-server   python -m bot.sentinel.guardian   (bot.main under the supervisor: restarts, watchdog)
#   abp          python -m abp_cli                  (the command line)
#   abp-modkit   python -m abp_modkit               (make any project a module: abp-modkit adopt <folder>)
#   abp-tui      python -m bot.tui                  (the terminal UI)
{ lib, stdenvNoCC, python3, makeWrapper, git, openssh, fetchPypi, version ? "0.0.0" }:

let
  py = python3.override {
    self = py;
    packageOverrides = pyself: pysuper: lib.optionalAttrs (!(pysuper ? httpx2)) {
      # mcp's HTTP client, which older nixpkgs do not package yet
      httpx2 = pyself.buildPythonPackage rec {
        pname = "httpx2";
        version = "2.13.1";
        pyproject = true;
        # The hash is filled in from the first build on NixOS (nix reports the real one).
        src = fetchPypi { inherit pname version; hash = lib.fakeHash; };
        build-system = with pyself; [ hatchling hatch-fancy-pypi-readme ];
        dependencies = with pyself; [ anyio certifi httpcore idna ];
        pythonImportsCheck = [ "httpx2" ];
        doCheck = false;
      };
    };
  };

  pythonEnv = py.withPackages (ps: with ps; [
    python-telegram-bot anthropic pyyaml psutil fastapi uvicorn uvloop httptools python-dotenv watchfiles mcp httpx
    httpx2 discordpy slack-bolt websockets python-multipart qrcode pypng pillow pyjwt cryptography numpy matrix-nio
    textual ruamel-yaml jsonschema zeroconf tzdata setuptools tkinter
    opencv4   # bot/vision: nixpkgs' OpenCV with contrib and its Python bindings (pip: opencv-contrib-python-headless)
  ]);

  # Only what ABP runs: no tests, no other platforms' apps, no build output or local state.
  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let rel = lib.removePrefix (toString ../. + "/") (toString path); in
      !(lib.any (p: rel == p || lib.hasPrefix (p + "/") rel) [
        ".git" ".venv" "venv" "data" "logs" "tests" "android-app" "browser-extension" "vendor" "graphify-out"
        "desktop-app/src-tauri/target" "node_modules" ".pytest_cache" ".ruff_cache"
        # config/backends.yaml is the tracked default that seeds a new ABP_HOME, so it stays; these hold keys:
        "config/providers.yaml" ".env"
      ])
      && !(lib.hasSuffix ".pyc" rel) && baseNameOf rel != "__pycache__";
  };
in
stdenvNoCC.mkDerivation {
  pname = "agentic-bot-platform";
  inherit version src;
  nativeBuildInputs = [ makeWrapper pythonEnv ];
  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    share=$out/share/agentic-bot-platform
    mkdir -p $share $out/bin
    cp -r . $share/
    # Compile once here: the store is read-only, so Python could never cache its bytecode later.
    ${pythonEnv}/bin/python -m compileall -q -j 0 $share/bot $share/abp_* >/dev/null || true
    for spec in "abp-server:bot.sentinel.guardian" "abp:abp_cli" "abp-tui:bot.tui" "abp-modkit:abp_modkit"; do
      name=''${spec%%:*}; mod=''${spec#*:}
      makeWrapper ${pythonEnv}/bin/python $out/bin/$name \
        --add-flags "-m $mod" \
        --prefix PYTHONPATH : $share \
        --prefix PATH : ${lib.makeBinPath [ git openssh ]} \
        --set-default PYTHONDONTWRITEBYTECODE 1 \
        --run 'export ABP_CALLER_CWD="$PWD" ABP_HOME="''${ABP_HOME:-''${XDG_DATA_HOME:-$HOME/.local/share}/agentic-bot-platform}"; mkdir -p "$ABP_HOME" && cd "$ABP_HOME"'
    done
    runHook postInstall
  '';

  passthru = { inherit pythonEnv; };

  meta = {
    description = "Run many independent chat bots and agents across platforms and backends from one app";
    homepage = "https://github.com/LoopyLuci/AgenticBotPlatform";
    license = lib.licenses.mit;
    mainProgram = "abp";
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
  };
}
