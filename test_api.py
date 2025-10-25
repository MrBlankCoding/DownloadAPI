import os
import json
import time
from fastapi.testclient import TestClient
from main import app, DOWNLOAD_DIR

# Test client
client = TestClient(app)

# Test with a reliable, non-restricted video (YouTube's "Me at the zoo" - first YouTube video)
TEST_VIDEO_ID = "0Wa_CR0H8g4"

def test_download_video():
    """Test downloading a video and verify the download process"""
    # Clean up any existing download before starting
    output_file = os.path.join(DOWNLOAD_DIR, f"{TEST_VIDEO_ID}.mp3")
    if os.path.exists(output_file):
        os.remove(output_file)
    
    try:
        # Test the download endpoint
        response = client.get(f"/download-progress/{TEST_VIDEO_ID}")
        assert response.status_code == 200, f"Expected status code 200, got {response.status_code}"
        
        # Read and process the streaming response
        content = ""
        for chunk in response.iter_lines():
            if chunk:
                content += chunk
                print(chunk)  # Debug output
                
                # Check for error status in the stream
                if '"status": "error"' in chunk or '"status":"error"' in chunk:
                    error_data = json.loads(chunk.replace('data: ', ''))
                    raise AssertionError(f"Download failed with error: {error_data.get('message', 'Unknown error')}")
        
        # Verify the download completed successfully (check for both spaced and non-spaced JSON)
        assert ('"status": "completed"' in content or '"status":"completed"' in content), \
            f"Download did not complete successfully. Last 500 chars: {content[-500:]}"
        
        # Verify the file was downloaded and has content
        assert os.path.exists(output_file), "Downloaded file not found"
        assert os.path.getsize(output_file) > 1024, "Downloaded file is too small"
        
    finally:
        # Clean up the downloaded file
        if os.path.exists(output_file):
            os.remove(output_file)


def test_video_info():
    """Test getting video information"""
    # Test the video info endpoint (correct path is /video-info/)
    response = client.get(f"/video-info/{TEST_VIDEO_ID}")
    assert response.status_code == 200, f"Expected status code 200, got {response.status_code}"
    
    data = response.json()
    assert "video_id" in data, "Response missing 'video_id' field"
    assert data["video_id"] == TEST_VIDEO_ID, f"Expected video ID {TEST_VIDEO_ID}, got {data.get('video_id')}"
    assert "title" in data, "Response missing 'title' field"
    assert data["title"], "Video title is empty"
    assert "duration" in data, "Response missing 'duration' field"
    assert data["duration"] > 0, "Invalid duration"


if __name__ == "__main__":
    import pytest
    pytest.main(["-v"])