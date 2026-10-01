"""ABP File Server: a NAS in software — disks pooled into shares, protected by parity, served to every device, moved
between machines, backed up, and watched by models.

    store      where the file server keeps its state (`<data>/fileserver/`, ABP_FILESERVER_DIR), FsError
    disks      every drive and volume on the machine, SMART where smartctl or the OS gives it, temperatures, and a
               failure-risk score per drive (ml.disk_risk)
    array      the parity array (Unraid's array, SnapRAID's method): data disks of any size and filesystem — each a
               folder on its own drive — plus one or two parity disks; sync, scrub (bitrot found by block hashes), fix
               and rebuild of a lost disk (single parity: XOR; dual: Reed-Solomon over GF(2^8), any two disks), and
               emulation (a lost disk's files read on the fly from parity)
    shares     user shares across the array's disks and a cache (Unraid's user shares): allocation (high-water, most
               free, fill-up), split level, include/exclude disks, cache use (no / yes / only / prefer); users,
               passwords and per-share access; share links with expiry and an optional password
    mover      moves files between the cache and the array as each share says, on a schedule or now; ml.tiering
               keeps hot files on the cache
    index      the content index: every file's type, size, hashes; text extracted, images described by bot/vision;
               search by words or meaning (local embeddings), exact and near duplicates (perceptual hashes)
    guard      watches changes for ransomware-like bursts (entropy jumps, mass renames to new extensions) and alerts
    transfer   resumable, verified, parallel transfers and sync jobs between this server, other ABP file servers,
               local folders, WebDAV and S3-compatible storage
    backup     deduplicated, compressed, encrypted, versioned backups with retention, restore and check
    server     the file server process: the web file manager, the REST file API (Range, resumable uploads), WebDAV
               (mount it as a drive on Windows, macOS, Linux, phones), share links
    exports    SMB / NFS exports through the operating system (the commands, run by the person with admin rights)
    apps       one-click containers that use the shares (Jellyfin, Nextcloud, Immich, Syncthing...), through
               bot/docker_mgr.py
    service    status, statistics, schedules (parity sync and scrub, mover, SMART, index, backups), notifications

The dashboard (bot/dashboard/fileserver_api.py), the Storage page, `abp nas ...`, the TUI's Storage screen and the
agents' nas_* tools drive this package.
"""
