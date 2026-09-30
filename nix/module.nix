# services.agentic-bot-platform: ABP as a NixOS system service.
#
#   services.agentic-bot-platform = {
#     enable = true;
#     host = "0.0.0.0";           # default 127.0.0.1 (this machine only)
#     openFirewall = true;         # only if host is not loopback
#     environmentFile = "/run/secrets/abp.env";   # optional: tokens and keys, never in the store
#   };
#
# State (the auto-generated dashboard token in .env, config, the database, logs) lives in dataDir, owned by the
# service's own user. The code is the read-only package. ABP runs under its supervisor (bot.sentinel.guardian),
# and systemd restarts the supervisor if it ever exits with an error.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.agentic-bot-platform;
  inherit (lib) mkEnableOption mkOption mkIf types optional;
  # Under /var/lib, systemd creates the state directory itself (StateDirectory), owned by the service user, before
  # it builds the sandbox; naming a missing directory in ReadWritePaths fails the first start (226/NAMESPACE).
  stateDir = lib.removePrefix "/var/lib/" cfg.dataDir;
  underVarLib = lib.hasPrefix "/var/lib/" cfg.dataDir;
in
{
  options.services.agentic-bot-platform = {
    enable = mkEnableOption "Agentic Bot Platform (ABP)";
    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "agentic-bot-platform.packages.\${system}.default";
      description = "The ABP package.";
    };
    user = mkOption { type = types.str; default = "abp"; description = "User the service runs as."; };
    group = mkOption { type = types.str; default = "abp"; description = "Group the service runs as."; };
    createUser = mkOption {
      type = types.bool;
      default = true;
      description = "Create `user` and `group` as a system account. Turn off to run as an account defined elsewhere.";
    };
    dataDir = mkOption {
      type = types.path;
      default = "/var/lib/abp";
      description = "ABP_HOME: the .env (with the dashboard token), config, database and logs.";
    };
    host = mkOption { type = types.str; default = "127.0.0.1"; description = "Address the dashboard and API listen on."; };
    port = mkOption { type = types.port; default = 8787; description = "Port the dashboard and API listen on."; };
    openFirewall = mkOption { type = types.bool; default = false; description = "Open the port in the firewall."; };
    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "A file of KEY=value lines (secrets) added to the service's environment; kept out of the store.";
    };
    extraPackages = mkOption {
      type = types.listOf types.package;
      default = [ ];
      example = lib.literalExpression "[ pkgs.docker pkgs.qemu ]";
      description = "Programs ABP may run (Docker, QEMU, tailscale, ...), put on the service's PATH.";
    };
  };

  config = mkIf cfg.enable {
    users.users = lib.optionalAttrs cfg.createUser {
      ${cfg.user} = { isSystemUser = true; group = cfg.group; home = cfg.dataDir; description = "Agentic Bot Platform"; };
    };
    users.groups = lib.optionalAttrs cfg.createUser { ${cfg.group} = { }; };

    systemd.tmpfiles.rules = lib.optional (!underVarLib) "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.group} - -";

    systemd.services.agentic-bot-platform = {
      description = "Agentic Bot Platform";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      path = [ pkgs.git pkgs.openssh pkgs.coreutils pkgs.bash ] ++ cfg.extraPackages;
      environment = {
        ABP_HOME = cfg.dataDir;
        HOME = cfg.dataDir;
        DASHBOARD_HOST = cfg.host;
        DASHBOARD_PORT = toString cfg.port;
        PYTHONDONTWRITEBYTECODE = "1";
      };
      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        ExecStart = "${cfg.package}/bin/abp-server";
        EnvironmentFile = optional (cfg.environmentFile != null) cfg.environmentFile;
        Restart = "on-failure";
        RestartSec = 10;
        # ABP starts programs (hubs, sandboxes, tools) as children; stopping the service stops all of them.
        KillMode = "control-group";
        TimeoutStopSec = 30;
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        StateDirectory = mkIf underVarLib stateDir;
        StateDirectoryMode = mkIf underVarLib "0750";
        ReadWritePaths = mkIf (!underVarLib) [ cfg.dataDir ];
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        UMask = "0077";
      };
    };

    networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];

    environment.systemPackages = [ cfg.package ];
  };
}
