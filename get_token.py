"""
Ek baar chalao (laptop ya Colab par) — client_secret.json same folder me rakho.
Output me jo JSON aayega, use GitHub secret `YT_TOKEN_JSON` me paste karo.
"""
import os
from urllib.parse import urlparse, parse_qs
from google_auth_oauthlib.flow import InstalledAppFlow

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT = "http://localhost:8080/"

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES, redirect_uri=REDIRECT)
url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")

print("1) Ye link browser me kholo, channel wale account se login karo:\n")
print(url)
print("\n2) Redirect page error dikhayega (normal). Address bar ki PURI URL copy karo.\n")

pasted = input("Paste URL (ya sirf code): ").strip()
code = pasted
if pasted.startswith("http"):
    code = parse_qs(urlparse(pasted).query)["code"][0]

flow.fetch_token(code=code)
print("\n===== YT_TOKEN_JSON (ye poora copy karo) =====\n")
print(flow.credentials.to_json())
print("\n==============================================")
