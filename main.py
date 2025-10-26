import os
import tempfile
import asyncio
import logging
from pathlib import Path
from typing import List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException, Request
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

Config.validate()

# Pydantic models
class VideoInfo(BaseModel):
    title: str
    video_id: str = Field(alias="videoId")
    channel_title: str = Field(alias="channelTitle")
    thumbnail: str
    duration: str
    
    class Config:
        populate_by_name = True

class SearchResponse(BaseModel):
    videos: List[VideoInfo]
    query: str
    count: int

# Global executor (initialized before lifespan)
executor = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS)

# Lifespan context manager for cleanup
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up Music Downloader API...")
    yield
    # Shutdown
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

# Initialize services
try:
    youtube_service = build("youtube", "v3", developerKey=Config.YOUTUBE_API_KEY)
except Exception as e:
    logger.error(f"Failed to initialize YouTube service: {e}")
    raise

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
            thumbnail=snippet["thumbnails"]["high"]["url"],
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

# Run with: uvicorn main:app --host 0.0.0.0 --port 8000 --reload