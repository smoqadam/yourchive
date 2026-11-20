# Yourchive

A self-hosted YouTube archive for videos, playlists, channels, subtitles, and metadata.

## Run

```sh
docker compose up -d --build
```

Open [http://localhost:8000](http://localhost:8000). Add YouTube links in the app. Videos and the library database persist in Docker volumes.

## Jellyfin

Start Yourchive and Jellyfin together with the included Compose profile:

```sh
docker compose --profile jellyfin up -d --build
```

Open Jellyfin at [http://localhost:8096](http://localhost:8096), finish setup, and create a **Music Videos** library rooted at `/media/videos`. In Yourchive Settings, enable Jellyfin sync, enter `http://jellyfin:8096`, your Jellyfin API key, and `/media` for the shared media path. Save, then choose **Sync archive to Jellyfin**. Future downloads sync automatically.

Yourchive writes an `.nfo` metadata file beside each video. Keep Jellyfin's `/media` mount read-only; both services share the same persistent media volume.

To keep an existing Jellyfin installation, mount the `archive-media` Docker volume read-only at `/media` in Jellyfin and use a Jellyfin URL reachable from the Yourchive container.

Set **Media path as seen by Jellyfin** to the mount target inside the Jellyfin container. For example, if Jellyfin mounts the shared folder at `/youtube`, set the media path to `/youtube` and point the Jellyfin library at `/youtube/videos`. If more than one Jellyfin account is enabled, enter the username that should own synced playlists in **Jellyfin playlist owner**.
