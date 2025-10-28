import os
import tempfile
import asyncio
import logging
from pathlib import Path
from typing import List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from concurrent.futures import ThreadPoolExecutor
import yt_dlp
from pydantic import BaseModel, Field
import json
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configuration
class Config:
    YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")
    COOKIES_FILE: str = os.getenv("YOUTUBE_COOKIES", "www.youtube.com_cookies.txt")
    DOWNLOAD_DIR: Path = Path("downloads")
    MAX_WORKERS: int = 5
    SEARCH_RATE_LIMIT: str = "10/minute"
    DOWNLOAD_RATE_LIMIT: str = "5/minute"
    MAX_SEARCH_RESULTS: int = 5
    
    @classmethod
    def validate(cls):
        if not cls.YOUTUBE_API_KEY:
            raise RuntimeError("Missing YOUTUBE_API_KEY in .env file")
        cls.DOWNLOAD_DIR.mkdir(exist_ok=True)
        if not Path(cls.COOKIES_FILE).exists():
            logger.warning(f"Cookies file not found: {cls.COOKIES_FILE}")

# Pydantic models
class VideoInfo(BaseModel):
    title: str
    video_id: str = Field(alias="videoId")
    channel_title: str = Field(alias="channelTitle")
    thumbnail: str
    duration: str
    
    class Config:
        populate_by_name = True

class PlaylistInfo(BaseModel):
    title: str
    playlist_id: str = Field(alias="playlistId")
    channel_title: str = Field(alias="channelTitle")
    thumbnail: str
    video_count: str = Field(alias="videoCount")
    
    class Config:
        populate_by_name = True

class SearchResponse(BaseModel):
    videos: List[VideoInfo]
    query: str
    count: int

class PlaylistSearchResponse(BaseModel):
    playlists: List[PlaylistInfo]
    query: str
    count: int

class PlaylistDownloadRequest(BaseModel):
    playlist_id: str
    title: str
    thumbnail: str
    channel_title: str

# Global executor (initialized before lifespan)
executor = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS)

# Playlist storage
class PlaylistStorage:
    def __init__(self):
        self.storage_file = Config.DOWNLOAD_DIR / "playlists.json"
        self.storage_file.parent.mkdir(exist_ok=True)
        self._playlists = {}
        self._load()
    
    def _load(self):
        if self.storage_file.exists():
            try:
                with open(self.storage_file, 'r') as f:
                    self._playlists = json.load(f)
            except Exception as e:
                logger.error(f"Error loading playlists: {e}")
                self._playlists = {}
    
    def _save(self):
        try:
            with open(self.storage_file, 'w') as f:
                json.dump(self._playlists, f)
        except Exception as e:
            logger.error(f"Error saving playlists: {e}")
    
    def create_playlist(self, playlist_id: str, title: str, thumbnail: str, channel_title: str):
        self._playlists[playlist_id] = {
            "id": playlist_id,
            "title": title,
            "thumbnail": thumbnail,
            "channel_title": channel_title,
            "songs": [],
            "created_at": datetime.now().isoformat(),
            "status": "downloading"
        }
        self._save()
    
    def add_song_to_playlist(self, playlist_id: str, song_data: dict):
        if playlist_id in self._playlists:
            self._playlists[playlist_id]["songs"].append(song_data)
            self._save()
    
    def complete_playlist(self, playlist_id: str):
        if playlist_id in self._playlists:
            self._playlists[playlist_id]["status"] = "completed"
            self._save()
    
    def get_playlist(self, playlist_id: str):
        return self._playlists.get(playlist_id)
    
    def get_all_playlists(self):
        return list(self._playlists.values())

# Download task manager
class DownloadTaskManager:
    def __init__(self, max_concurrent=2):
        self.max_concurrent = max_concurrent
        self.tasks = {}
        self.semaphore = asyncio.Semaphore(max_concurrent)
    
    async def download_playlist_songs(self, playlist_id: str, video_ids: List[str], temp_dir: str, websocket_manager=None):
        """Download playlist songs with concurrency control."""
        completed = 0
        total = len(video_ids)
        
        async def download_with_limit(video_id: str, index: int):
            nonlocal completed
            async with self.semaphore:
                try:
                    url = f"https://www.youtube.com/watch?v={video_id}"
                    logger.info(f"Downloading song {index+1}/{total} from playlist {playlist_id}")
                    
                    # Send progress update
                    if websocket_manager:
                        await websocket_manager.broadcast(json.dumps({
                            "type": "playlist_progress",
                            "playlist_id": playlist_id,
                            "current": index + 1,
                            "total": total,
                            "status": "downloading",
                            "message": f"Downloading song {index+1} of {total}"
                        }))
                    
                    ydl_opts = get_ydl_options(temp_dir)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        base_filename = ydl.prepare_filename(info)
                        filename = os.path.splitext(base_filename)[0] + ".mp3"
                    
                    completed += 1
                    logger.info(f"Completed download {completed}/{total}: {filename}")
                    
                    # Send completion update for this song
                    if websocket_manager:
                        await websocket_manager.broadcast(json.dumps({
                            "type": "song_completed",
                            "playlist_id": playlist_id,
                            "completed": completed,
                            "total": total,
                            "video_id": video_id,
                            "song_title": info.get('title', 'Unknown'),
                            "message": f"Downloaded: {info.get('title', 'Unknown')}"
                        }))
                    
                    return filename
                except Exception as e:
                    logger.error(f"Download failed for {video_id}: {e}")
                    if websocket_manager:
                        await websocket_manager.broadcast(json.dumps({
                            "type": "song_failed",
                            "playlist_id": playlist_id,
                            "video_id": video_id,
                            "error": str(e),
                            "message": f"Failed to download song {index+1}"
                        }))
                    return None
        
        tasks = [download_with_limit(vid, idx) for idx, vid in enumerate(video_ids)]
        results = await asyncio.gather(*tasks)
        
        return [r for r in results if r is not None]

playlist_storage = PlaylistStorage()
task_manager = DownloadTaskManager(max_concurrent=2)

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except:
            self.disconnect(websocket)

    async def broadcast(self, message: str):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.error(f"Failed to send WebSocket message: {e}")
                disconnected.append(connection)
        
        # Remove disconnected connections
        for connection in disconnected:
            self.disconnect(connection)

manager = ConnectionManager()

# Lifespan context manager for cleanup
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up Music Downloader API...")
    
    # Validate config here (runtime-safe)
    try:
        Config.validate()
        logger.info("Configuration validated successfully.")
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        raise

    global youtube_service
    try:
        youtube_service = build("youtube", "v3", developerKey=Config.YOUTUBE_API_KEY)
        logger.info("YouTube service initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize YouTube service: {e}")
        raise

    yield

    logger.info("Shutting down... cleaning up resources")
    executor.shutdown(wait=True)


# Initialize FastAPI app
app = FastAPI(
    title="Music Downloader API",
    version="2.2",
    description="API for searching and downloading music from YouTube",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# YouTube service will be initialized in lifespan
youtube_service = None

# Rate limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# -------------------------
# Helper Functions
# -------------------------

def get_ydl_options(temp_dir: str) -> dict:
    """Get yt-dlp options configuration."""
    options = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192"
            },
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata"},
        ],
        "writethumbnail": True,
        "addmetadata": True,
        "quiet": True,
        "no_warnings": True,
    }
    
    # Only add cookies if file exists
    if Path(Config.COOKIES_FILE).exists():
        options["cookies"] = Config.COOKIES_FILE
    
    return options


def get_thumbnail_url(thumbnails: dict) -> str:
    """Return the best available thumbnail URL from a YouTube snippet thumbnails dict.

    Tries a list of preferred keys ('high', 'standard', 'medium', 'default') and
    falls back to the first available thumbnail entry. Returns empty string when
    nothing is available.
    """
    if not thumbnails or not isinstance(thumbnails, dict):
        return ""

    for key in ("high", "standard", "medium", "default"):
        entry = thumbnails.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            return entry["url"]

    # Fallback: return the first URL we can find
    for entry in thumbnails.values():
        if isinstance(entry, dict) and entry.get("url"):
            return entry["url"]

    return ""

def download_audio_sync(video_id: str) -> str:
    """Synchronous download using yt-dlp with cookies support."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    temp_dir = tempfile.mkdtemp(dir=Config.DOWNLOAD_DIR)
    
    logger.info(f"Starting download for video ID: {video_id}")
    print(f"Starting download for video ID: {video_id}")
    
    try:
        ydl_opts = get_ydl_options(temp_dir)
        print(f"ydl_opts: {ydl_opts}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Get the base filename and replace extension with .mp3
            base_filename = ydl.prepare_filename(info)
            filename = os.path.splitext(base_filename)[0] + ".mp3"
        
        logger.info(f"Download completed: {filename}")
        print(f"Download completed: {filename}")
        return filename
    except Exception as e:
        logger.error(f"Download failed for {video_id}: {e}")
        print(f"Download failed for {video_id}: {e}")
        # Clean up temp directory on failure
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

async def download_audio_async(video_id: str) -> str:
    """Asynchronous wrapper for download_audio_sync."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, download_audio_sync, video_id)

async def get_video_details(video_id: str) -> str:
    """Asynchronously fetch video duration from YouTube API."""
    try:
        loop = asyncio.get_event_loop()
        details = await loop.run_in_executor(
            executor,
            lambda: youtube_service.videos().list(  # pylint: disable=no-member
                part="contentDetails", id=video_id
            ).execute()
        )
        
        items = details.get("items", [])
        if items:
            content_details = items[0].get("contentDetails", {})
            duration = content_details.get("duration", "Unknown")
            return duration
        
        logger.warning(f"No items found for video ID: {video_id}")
        return "Unknown"
        
    except HttpError as e:
        logger.error(f"YouTube API HttpError for {video_id}: {e}")
        return "Unknown"
    except Exception as e:
        logger.error(f"Unexpected error fetching details for {video_id}: {e}")
        return "Unknown"

# -------------------------
# Routes
# -------------------------

@app.get("/", tags=["Health"])
def root():
    """Health check endpoint."""
    return {
        "status": "online",
        "message": "Music Downloader API is running",
        "version": "2.2"
    }

@app.get("/health", tags=["Health"])
def health_check():
    """Detailed health check."""
    return {
        "status": "healthy",
        "youtube_api": "connected" if Config.YOUTUBE_API_KEY else "not configured",
        "cookies": "available" if Path(Config.COOKIES_FILE).exists() else "not found"
    }

@app.get("/search", response_model=SearchResponse, tags=["Search"])
@limiter.limit(Config.SEARCH_RATE_LIMIT)
async def search_song(
    request: Request,
    query: str = Query(..., description="Search query for the song", min_length=1)
):
    """
    Search for songs on YouTube.
    
    - **query**: Search term (e.g., artist name, song title)
    - Returns up to 5 video results with metadata
    """
    logger.info(f"Search request: '{query}' from {get_remote_address(request)}")
    
    try:
        search_request = youtube_service.search().list(
            part="snippet",  # pylint: disable=no-member
            q=query,
            maxResults=Config.MAX_SEARCH_RESULTS,
            type="video",
            videoCategoryId="10",  # Music category
            safeSearch="moderate"
        )
        response = search_request.execute()
    except HttpError as e:
        logger.error(f"YouTube API error during search: {e}")
        raise HTTPException(
            status_code=503,
            detail="YouTube API service temporarily unavailable"
        )
    except Exception as e:
        logger.error(f"Unexpected error during search: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    videos = []
    for item in response.get("items", []):
        video_id = item["id"]["videoId"]
        snippet = item["snippet"]
        duration = await get_video_details(video_id)

        videos.append(VideoInfo(
            title=snippet["title"],
            videoId=video_id,
            channelTitle=snippet["channelTitle"],
            thumbnail=get_thumbnail_url(snippet.get("thumbnails", {})),
            duration=duration
        ))

    return SearchResponse(videos=videos, query=query, count=len(videos))

@app.get("/download/{video_id}", tags=["Download"])
@limiter.limit(Config.DOWNLOAD_RATE_LIMIT)
async def download_audio(
    video_id: str,
    request: Request
):
    """
    Download audio from a YouTube video.
    
    - **video_id**: YouTube video ID (e.g., 'dQw4w9WgXcQ')
    - Returns MP3 file
    """
    logger.info(f"Download request for {video_id} from {get_remote_address(request)}")
    print(f"Download request for {video_id}")
    
    # Basic validation
    if not video_id or len(video_id) != 11:
        raise HTTPException(status_code=400, detail="Invalid video ID format")
    
    try:
        filename = await download_audio_async(video_id)
        print(f"filename: {filename}")
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp download error: {e}")
        print(f"yt-dlp download error: {e}")
        raise HTTPException(
            status_code=404,
            detail="Video not found or unavailable for download"
        )
    except Exception as e:
        logger.error(f"Download failed for {video_id}: {e}")
        print(f"Download failed for {video_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Download failed: {str(e)}"
        )

    if not os.path.exists(filename):
        print(f"File not created: {filename}")
        raise HTTPException(status_code=500, detail="File was not created")

    return FileResponse(
        filename,
        media_type="audio/mpeg",
        filename=os.path.basename(filename),
        headers={"Content-Disposition": f"attachment; filename={os.path.basename(filename)}"}
    )

@app.get("/search_playlists", response_model=PlaylistSearchResponse, tags=["Search"])
@limiter.limit(Config.SEARCH_RATE_LIMIT)
async def search_playlists(
    request: Request,
    query: str = Query(..., description="Search query for playlists", min_length=1)
):
    """
    Search for playlists on YouTube.
    
    - **query**: Search term (e.g., artist name + "album" or playlist title)
    - Returns up to 5 playlist results with metadata
    """
    logger.info(f"Playlist search request: '{query}' from {get_remote_address(request)}")
    
    try:
        search_request = youtube_service.search().list(
            part="snippet",  # pylint: disable=no-member
            q=query,
            maxResults=Config.MAX_SEARCH_RESULTS,
            type="playlist",
        )
        response = search_request.execute()
    except HttpError as e:
        logger.error(f"YouTube API error during playlist search: {e}")
        raise HTTPException(
            status_code=503,
            detail="YouTube API service temporarily unavailable"
        )
    except Exception as e:
        logger.error(f"Unexpected error during playlist search: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    playlists = []
    for item in response.get("items", []):
        playlist_id = item["id"]["playlistId"]
        snippet = item["snippet"]
        
        # Get video count from playlist details
        try:
            playlist_details = youtube_service.playlists().list(
                part="contentDetails",
                id=playlist_id
            ).execute()
            
            video_count = playlist_details.get("items", [{}])[0].get("contentDetails", {}).get("itemCount", "0")
        except Exception:
            video_count = "0"

        playlists.append(PlaylistInfo(
            title=snippet["title"],
            playlistId=playlist_id,
            channelTitle=snippet["channelTitle"],
            thumbnail=get_thumbnail_url(snippet.get("thumbnails", {})),
            videoCount=str(video_count)
        ))

    return PlaylistSearchResponse(playlists=playlists, query=query, count=len(playlists))

async def fetch_playlist_videos(playlist_id: str):
    """Helper function to fetch all videos from a playlist."""
    logger.info(f"Fetching videos from playlist {playlist_id}")
    
    try:
        videos = []
        next_page_token = None
        
        while True:
            request_obj = youtube_service.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=next_page_token
            )
            response = request_obj.execute()
            
            for item in response.get("items", []):
                video_id = item["contentDetails"]["videoId"]
                snippet = item["snippet"]
                videos.append({
                    "videoId": video_id,
                    "title": snippet["title"],
                    "channelTitle": snippet["channelTitle"],
                    "thumbnail": snippet["thumbnails"]["high"]["url"]
                })
            
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
        
        return {"videos": videos, "count": len(videos)}
    
    except HttpError as e:
        logger.error(f"YouTube API error: {e}")
        raise
    except Exception as e:
        logger.error(f"Error fetching playlist videos: {e}")
        raise

@app.get("/playlist_videos/{playlist_id}", tags=["Search"])
@limiter.limit(Config.SEARCH_RATE_LIMIT)
async def get_playlist_videos(
    request: Request,
    playlist_id: str
):
    """
    Get all video IDs from a playlist.
    
    - **playlist_id**: YouTube playlist ID
    - Returns list of video IDs
    """
    try:
        return await fetch_playlist_videos(playlist_id)
    except HttpError as e:
        logger.error(f"YouTube API error: {e}")
        raise HTTPException(
            status_code=503,
            detail="YouTube API service temporarily unavailable"
        )
    except Exception as e:
        logger.error(f"Error fetching playlist videos: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

async def download_and_process_playlist(
    playlist_id: str, 
    title: str, 
    thumbnail: str, 
    channel_title: str
):
    """Background task to download a playlist."""
    try:
        # Send playlist start message
        await manager.broadcast(json.dumps({
            "type": "playlist_started",
            "playlist_id": playlist_id,
            "title": title,
            "message": f"Started downloading playlist: {title}"
        }))
        
        # Get all video IDs from the playlist
        videos_response = await fetch_playlist_videos(playlist_id)
        video_ids = [v["videoId"] for v in videos_response["videos"]]
        
        if not video_ids:
            logger.warning(f"Playlist {playlist_id} has no videos")
            await manager.broadcast(json.dumps({
                "type": "playlist_failed",
                "playlist_id": playlist_id,
                "error": "No videos found in playlist",
                "message": f"Playlist {title} has no videos"
            }))
            return
        
        # Create temp directory for downloads
        temp_dir = tempfile.mkdtemp(dir=Config.DOWNLOAD_DIR)
        
        # Download all songs with WebSocket updates
        downloaded_files = await task_manager.download_playlist_songs(
            playlist_id, video_ids, temp_dir, manager
        )
        
        # Add each downloaded song to the playlist
        for vid_data, file_path in zip(videos_response["videos"], downloaded_files):
            if file_path:
                song_data = {
                    "title": vid_data["title"],
                    "channel_title": vid_data["channelTitle"],
                    "thumbnail": vid_data["thumbnail"],
                    "video_id": vid_data["videoId"],
                    "file_path": file_path,
                    "downloaded_at": datetime.now().isoformat()
                }
                playlist_storage.add_song_to_playlist(playlist_id, song_data)
        
        # Mark playlist as complete
        playlist_storage.complete_playlist(playlist_id)
        logger.info(f"Playlist {playlist_id} download completed")
        
        # Send playlist completion message
        await manager.broadcast(json.dumps({
            "type": "playlist_completed",
            "playlist_id": playlist_id,
            "title": title,
            "total_songs": len(downloaded_files),
            "successful_downloads": len([f for f in downloaded_files if f]),
            "message": f"Playlist '{title}' download completed! {len([f for f in downloaded_files if f])} songs downloaded."
        }))
    
    except Exception as e:
        logger.error(f"Error downloading playlist {playlist_id}: {e}")
        if playlist_id in playlist_storage._playlists:
            playlist_storage._playlists[playlist_id]["status"] = "failed"
            playlist_storage._save()
        
        # Send playlist failure message
        await manager.broadcast(json.dumps({
            "type": "playlist_failed",
            "playlist_id": playlist_id,
            "title": title,
            "error": str(e),
            "message": f"Failed to download playlist: {title}"
        }))

@app.post("/download_playlist", tags=["Download"])
@limiter.limit(Config.DOWNLOAD_RATE_LIMIT)
async def download_playlist(
    request: Request,
    download_request: PlaylistDownloadRequest
):
    """
    Start downloading a playlist.
    
    - Creates a new playlist entry
    - Starts background download of all songs
    - Returns immediately with playlist info
    
    Downloads happen in background with concurrent limit (2-3 at a time).
    """
    logger.info(f"Playlist download request for '{download_request.title}'")
    
    # Create playlist entry
    playlist_storage.create_playlist(
        download_request.playlist_id,
        download_request.title,
        download_request.thumbnail,
        download_request.channel_title
    )
    
    # Start background download task
    asyncio.create_task(download_and_process_playlist(
        download_request.playlist_id,
        download_request.title,
        download_request.thumbnail,
        download_request.channel_title
    ))
    
    return {
        "status": "started",
        "playlist_id": download_request.playlist_id,
        "message": "Playlist download started in background"
    }

@app.get("/playlist_status/{playlist_id}", tags=["Download"])
async def get_playlist_status(playlist_id: str):
    """
    Get the current status of a playlist download.
    """
    playlist = playlist_storage.get_playlist(playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    return {
        "playlist_id": playlist_id,
        "title": playlist["title"],
        "status": playlist["status"],
        "songs_count": len(playlist.get("songs", [])),
        "created_at": playlist["created_at"]
    }

@app.get("/downloads", tags=["Download"])
async def get_all_downloads():
    """Get all downloaded playlists."""
    return {"playlists": playlist_storage.get_all_playlists()}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time playlist download updates."""
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive and listen for any messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# ngrok http --domain=lasandra-sultriest-bumblingly.ngrok-free.dev 8000