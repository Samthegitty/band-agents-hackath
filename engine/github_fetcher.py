# engine/github_fetcher.py
import os
import re
import requests
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

log = logging.getLogger("vyala_archon")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

def get_repo_files(repo_url: str) -> dict[str, str]:
    """
    Fetches source code files from a public GitHub repo using the REST API.
    Returns a dictionary of {filepath: file_content}.
    """
    # Parse owner and repo name from URL
    match = re.search(r"github\.com/([^/]+)/([^/]+)", repo_url)
    if not match:
        log.error("Invalid GitHub URL")
        return {}
    
    owner, repo = match.group(1), match.group(2)
    # Remove .git from end if user included it
    repo = repo.replace(".git", "") 
    
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    # Try both 'main' and 'master' branches
    tree_data = None
    for branch in ["main", "master"]:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        
        log.info(f"Trying branch '{branch}' from {api_url}...")
        resp = requests.get(api_url, headers=headers, timeout=10)
        
        if resp.status_code == 200:
            tree_data = resp.json().get("tree", [])
            break # Found the working branch!
        elif resp.status_code == 403:
            log.error("GitHub API Rate Limit Hit. Add GITHUB_TOKEN to .env!")
            return {}
            
    if not tree_data:
        log.error(f"Failed to fetch repo tree for {owner}/{repo}. Tried main and master.")
        return {}

    # Filter for source code files only
    allowed_extensions = ['.py', '.js', '.ts', '.go', '.java', '.rb', '.cs']
    files_to_scan = []
    
    for node in tree_data:
        if node["type"] == "blob":
            ext = os.path.splitext(node["path"])[1]
            if ext in allowed_extensions:
                files_to_scan.append(node["path"])

    log.info(f"Found {len(files_to_scan)} source files to scan.")

    # Fetch content for each file (limit to first 25 for local demo speed)
    files_content = {}
    
    # We need to figure out which branch worked to build the raw URL
    working_branch = "main" # default fallback
    if "master" in resp.url: # check which branch actually succeeded in the loop
        working_branch = "master"
        
    raw_url_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{working_branch}"
    
    for path in files_to_scan[:50]:
        raw_url = f"{raw_url_base}/{path}"
        try:
            file_resp = requests.get(raw_url, headers=headers, timeout=5)
            if file_resp.status_code == 200:
                files_content[path] = file_resp.text
        except Exception as e:
            log.warning(f"Could not fetch {path}: {e}")

    return files_content