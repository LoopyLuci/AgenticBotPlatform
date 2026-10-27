"""Video Studio engine (V1): the system every video feature uses.

    store      data/video ($ABP_VIDEO_DIR): the SQLite database (jobs, assets) and the files
    jobs       the durable job queue: queued -> waiting_gpu -> rendering -> done / failed / cancelled, one at a time,
               GPU-lock and VRAM aware, resumed after a restart; settings
    backends   the render backend interface: stable-diffusion.cpp (Vulkan), a test-pattern fake, ComfyUI (not yet)
    workflows  versioned JSON workflow templates with typed parameters (bot/video/workflows/*.json)
    registry   the model registry: files with SHA-256, verified on disk, downloaded on demand
    media      the ffmpeg layer: probe, transcode, trim, concat, audio mux, frames <-> video, thumbnails, previews
    library    the asset library: prompt, seed, model, workflow version, parent (lineage) and provenance
    tools      the video_* agent tools

Routes: bot/dashboard/video_api.py (/api/video/*). Workspace card + dialog: video.js. Unit `video` (bot/runtime/legacy.py)."""
