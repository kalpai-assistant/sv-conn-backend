import ssl
import urllib.request

# The actual file URL extracted from your extension link
download_url = (
    "https://cloudconvert.com"
    "No%20More%20Mr.%20Nice%20Guy%20by%20Robert%20Glover.epub"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Content-Sha256=UNSIGNED-PAYLOAD"
    "&X-Amz-Credential=cloudconvert-production%2F20260620%2Ffra%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260620T121434Z"
    "&X-Amz-Expires=86400"
    "&X-Amz-Signature=a2ebd3782ac8ac739aa7794f36e5ff1e7c68a497748cfef80e728efdf2a10a03"
    "&X-Amz-SignedHeaders=host"
    "&response-content-disposition=attachment%3B%20filename%3D%22No%20More%20Mr.%20Nice%20Guy%20by%20Robert%20Glover.epub%22"
    "&response-content-type=application%2Fepub%2Bzip"
    "&x-id=GetObject"
)

output_filename = "No_More_Mr_Nice_Guy.epub"

print("Downloading your file... Bypassing SSL verification.")

try:
    # Create a context that ignores the missing certificate authority
    context = ssl._create_unverified_context()

    # Pass the context into the download function
    with urllib.request.urlopen(download_url, context=context) as response:
        with open(output_filename, "wb") as out_file:
            out_file.write(response.read())

    print(f"Success! Saved as '{output_filename}' in your current folder.")
except Exception as e:
    print(f"Error downloading file: {e}")

