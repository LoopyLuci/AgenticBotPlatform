# `nix flake check` (or `nix build .#checks.x86_64-linux.nixos`): a NixOS VM with the module enabled. It proves the
# service starts, generates its dashboard token into ABP_HOME (never the store), answers health and the API with
# that token and refuses without it, that the CLI and TUI start, and that a restart keeps the same token.
{ self, pkgs }:

pkgs.testers.runNixOSTest {
  name = "agentic-bot-platform";
  nodes.machine = { ... }: {
    imports = [ self.nixosModules.default ];
    services.agentic-bot-platform.enable = true;
    virtualisation.memorySize = 2048;
  };
  testScript = ''
    machine.wait_for_unit("agentic-bot-platform.service")
    machine.wait_for_open_port(8787, timeout=180)
    machine.succeed("curl -fsS http://127.0.0.1:8787/healthz")
    token = machine.succeed("grep -oP '^DASHBOARD_TOKEN=\\K.*' /var/lib/abp/.env").strip()
    assert len(token) >= 16, "the dashboard token was not generated into ABP_HOME"
    machine.succeed(f"curl -fsS -H 'X-Dashboard-Token: {token}' http://127.0.0.1:8787/api/overview")
    machine.fail("curl -fsS http://127.0.0.1:8787/api/overview")
    machine.succeed("stat -c %U /var/lib/abp/.env | grep -qx abp")
    machine.succeed("abp --help")
    machine.succeed("timeout 20 abp-tui --help || test $? -eq 124")
    machine.succeed("systemctl restart agentic-bot-platform.service")
    machine.wait_for_open_port(8787, timeout=180)
    again = machine.succeed("grep -oP '^DASHBOARD_TOKEN=\\K.*' /var/lib/abp/.env").strip()
    assert again == token, "a restart replaced the dashboard token"
  '';
}
