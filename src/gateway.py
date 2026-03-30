from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LLM-Gateway")

app = FastAPI(title="OpenClaw LLM Security Gateway")

# We will route actual LLM queries to a target after review
# In a real scenario, OpenShell injects credentials locally, but we might need real endpoints.
OPENAI_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

def security_review(payload: dict) -> bool:
    """
    Perform security review on the LLM payload.
    For MVP, we use naive heuristic word matching.
    Returns True if safe, False if malicious/leak detected.
    """
    messages = payload.get("messages", [])
    if not messages:
        return True # Non-chat payload or Anthropic format requires different parsing
        
    for msg in messages:
        content = msg.get("content", "").lower()
        if any(bad_word in content for bad_word in ["rm -rf", "delete everything", "drop table", "upload all files to"]):
            logger.warning("Security Policy Violation detected in prompt.")
            return False
    return True

@app.post("/v1/{provider_path:path}")
async def proxy_llm(provider_path: str, request: Request):
    """
    Intercept LLM Requests from OpenShell Inference Gateway.
    """
    body = await request.json()
    
    logger.info(f"Intercepted LLM Request for {provider_path}")
    
    # 1. Security Review Phase
    if not security_review(body):
        return JSONResponse(status_code=403, content={"error": "Blocked by Security Guardrail"})
        
    # 2. Forward to Actual Provider
    # (Assuming OpenShell has already injected auth headers if acting as provider, 
    # OR we need to inject them here depending on OpenShell's exact inference provider routing).
    headers = dict(request.headers)
    
    # Clean up proxy-specific headers
    headers.pop("host", None)
    
    # Determine upstream URL (simplified)
    # The actual upstream relies on how the CLI provisions OpenClaw.
    url = f"https://api.openai.com/v1/{provider_path}" 

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except httpx.RequestError as exc:
            logger.error(f"Error forwarding request: {exc}")
            raise HTTPException(status_code=500, detail="Failed to contact upstream AI model")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway:app", host="0.0.0.0", port=8000, reload=True)
