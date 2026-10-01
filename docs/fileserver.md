# ABP File Server

A NAS in software. Disks of any size are pooled into shares and protected by parity. The shares are served to every device, moved between machines, backed up, and watched by models. Everything runs on this machine.

You can drive it from:

- the **Storage** page (dashboard and desktop app);
- `abp nas …`;
- the TUI's Storage screen (`f`);
- MCP (`nas_status`, `nas_search`, `nas_disks`, `nas_duplicates`);
- the agents' `nas_*` tools.

The code lives in `bot/fileserver/`. Management goes through `bot/dashboard/fileserver_api.py` (`/api/fileserver`). The files themselves are served by the file server's own process, `python -m bot.fileserver.server`, on port 8790 by default.

## Parity with Unraid and similar systems

| Feature | ABP File Server |
|---------|-----------------|
| Array of mixed-size disks, 1 or 2 parity disks | `array`: snapshot parity, as in SnapRAID. P is XOR; Q is Reed-Solomon over GF(2^8), so any two disks can be lost. Each data disk is a folder on its own drive. |
| Parity check / bitrot detection | `scrub`: every block has a BLAKE2b hash. Corrupted blocks are found, and `fix` repairs them. Wrong parity is rewritten. |
| Disk emulation while a disk is missing | Files of a missing disk are listed and served from parity (`X-ABP-Emulated`), byte-identical. |
| Rebuild onto a replacement drive | `fix --disk d1 --target <new folder>`. Every rebuilt block is checked against its hash before it is written. |
| User shares across disks | Shares see one tree over every disk and the cache. Settings: allocation (high-water, most-free, fill-up), split level, included/excluded disks, minimum free space. |
| Cache pools and the mover | Cache `yes`, `only` or `prefer`. The mover runs on a schedule. Tiering keeps recently read files on the cache, using the access log. |
| SMB / NFS exports | The operating system's own servers. ABP generates the exact commands (`New-SmbShare`, a Samba config, `/etc/exports`) and the person runs them with admin rights. |
| Web access, mobile access | A built-in web file manager: browse, chunked resumable uploads with SHA-256 checks, previews, thumbnails, search. Also WebDAV (class 2, with locks), so a share can be mounted as a drive. |
| Users and share security | public / secure / private, with r/rw per user. Passwords are PBKDF2-hashed. Sign-in is throttled. Sessions are HMAC-signed cookies. |
| Share links | Optional password, expiry, download limit, and uploads into a folder. |
| Recycle bin | Per disk, so a delete never moves data across drives. Purged after `recycle_days`. |
| Docker apps (Community Applications) | 20 templates: Jellyfin, Plex, Immich, PhotoPrism, Nextcloud, Syncthing, Paperless-ngx, Vaultwarden, Home Assistant, Pi-hole, AdGuard, the *arr apps, Gitea and more. Each is a compose stack through `bot/docker_mgr.py`, with share mounts. |
| VMs | ABP's existing VM manager (`bot/vm_mgr.py`, the Infra page). |
| Drive health, SMART, temperatures | `disks` reads smartctl if it is installed, otherwise Windows storage cmdlets or Linux lsblk/sysfs. Results are kept as history so trends show. |
| Notifications and a dashboard | An event log (alerts, warnings, finished jobs), plus CPU, memory, network and disk I/O. |
| Scheduled parity checks | Daily sync, scrubs every N days of a percentage of the array, mover, SMART, index, guard, transfers and backups. |
| Replication and sync (TrueNAS, Synology, Syncthing) | `transfer`: copy, mirror, or two-way with conflict copies. Endpoints: a local folder, a share, another ABP file server, WebDAV, or S3-compatible storage. Transfers run in parallel, retry, resume, can be rate-limited, and are verified. |
| Versioned backups (Hyper Backup, restic) | `backup`: deduplicated, zlib-compressed and AES-256-GCM-encrypted. Chunk ids are a keyed HMAC, so they leak no fingerprints. Retention keeps last/daily/weekly/monthly. Also prune, check (a sample or every chunk), and restore. |

## Models, all local

- **Content index:** for each file it records the kind and extracts text (plain text, code, Word, OpenDocument, HTML). Images are described by ABP's own vision models (objects, faces, QR codes, OCR); OpenCV is limited to 2 threads and work runs in budgeted slices. It also stores a 64-bit DCT perceptual hash and an embedding vector.
- **Search:**
  - **words:** SQLite FTS5/BM25.
  - **meaning:** cosine similarity over embeddings. The embedder is, in order: ABP's own local runtime (`ABP_EMBED_URL`), a local Ollama embedding model, or built-in hashed TF-IDF.
  - **auto:** both, fused by reciprocal rank.
- **Duplicates:** exact matches by SHA-256 (hashed only for files that share a size), and near-identical photos by perceptual hash. Near matches are found with banded LSH and union-find.
- **Failure risk per drive:** a logistic score over the SMART signals that Backblaze's published analyses tie to failure (5, 187, 188, 197, 198), NVMe health, the OS verdict, age, temperature and rising counts. The weights come from those findings and were not trained here. The score explains itself.
- **Ransomware guard:** it scores four signals:
  - a change rate against a learned baseline (EWMA z-score);
  - files whose entropy jumped from structured to random;
  - renames to new extensions;
  - ransom notes.

  With `auto_freeze`, the affected shares become read-only for everyone except administrators until a person unfreezes them.
- **Tiering:** recency and frequency of reads (the access log) keep hot files on the cache.

## What has been verified, and how

- **`tests/test_fileserver_array.py`, on real files:**
  - GF(2^8) recovery of every single and double loss;
  - sync/scrub through changes;
  - bitrot found and repaired;
  - a lost disk served from parity and rebuilt onto a new drive;
  - two lost disks rebuilt with dual parity, and one with Q alone;
  - too many losses refused, with nothing half-written;
  - removing a disk.
- **`tests/test_fileserver.py`:**
  - shares, allocation, split level, the mover and the recycle bin;
  - access, users, throttling and freezing;
  - the REST API: resumable uploads, refusing a checksum mismatch, Range, links with password, limits and uploads;
  - WebDAV: PROPFIND, MKCOL, PUT, LOCK enforcement, MOVE, COPY;
  - the index (words, meaning, exact and near duplicates);
  - the guard catching a simulated encryption burst and freezing the share;
  - backups (dedupe, encryption, restore, retention, prune, a damaged chunk detected);
  - transfers (mirror, two-way with a conflict, resume of a partial file);
  - the dashboard API, the pages, the tools and the CLI.
- **Live:**
  - the real server process and its web UI in a browser (browse, thumbnail, preview);
  - a data disk removed while running: its file is still listed and served from parity, byte-identical;
  - the drive inventory on this machine: 5 drives with model, bus and OS health.
- **Not yet run live:** WebDAV from Windows Explorer and macOS Finder (Windows needs HTTPS for basic auth: put the server behind ABP Hosting); S3 and WebDAV remotes against real services; app installs, which pull images and need the person's go-ahead.
