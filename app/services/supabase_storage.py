import os
import logging
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client, Client

# Explicitly load .env file into os.environ
load_dotenv()

logger = logging.getLogger("agent.storage")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "citizen-documents").strip()


class SupabaseStorageService:
    def __init__(self):
        if SUPABASE_URL and SUPABASE_SERVICE_KEY:
            try:
                self.client: Optional[Client] = create_client(
                    SUPABASE_URL,
                    SUPABASE_SERVICE_KEY
                )
                self.bucket_name = SUPABASE_STORAGE_BUCKET
                logger.info("Supabase client initialized successfully.")
                self._ensure_bucket_exists()
            except Exception as e:
                self.client = None
                self.bucket_name = SUPABASE_STORAGE_BUCKET
                logger.error(f"Failed to initialize Supabase client: {str(e)}")
        else:
            self.client = None
            self.bucket_name = SUPABASE_STORAGE_BUCKET
            logger.error("Supabase Storage credentials not found. Check SUPABASE_URL and SUPABASE_SERVICE_KEY in .env.")

    def _ensure_bucket_exists(self):
        if not self.client:
            return
        try:
            buckets = self.client.storage.list_buckets()
            bucket_names = [b.name for b in buckets]
            if self.bucket_name not in bucket_names:
                self.client.storage.create_bucket(
                    self.bucket_name,
                    options={"public": False}
                )
                logger.info(f"Created private Supabase Storage bucket: '{self.bucket_name}'")
        except Exception as e:
            logger.error(f"Error checking/creating storage bucket: {str(e)}")

    def upload_file(self, destination_path: str, file_bytes: bytes, content_type: str) -> str:
        if not self.client:
            raise RuntimeError("Supabase client is not initialized. Check your credentials.")

        self.client.storage.from_(self.bucket_name).upload(
            path=destination_path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "true"}
        )
        return destination_path

    def get_signed_url(
        self,
        file_path: Optional[str] = None,
        expires_in_seconds: int = 3600,
        **kwargs
    ) -> Optional[str]:
        """
        Generates a temporary signed download URL.
        Accepts both 'file_path' and 'storage_path' to prevent keyword argument mismatches.
        """
        if not self.client:
            return None

        # Resolve path whether called as file_path or storage_path
        target_path = file_path or kwargs.get("storage_path") or kwargs.get("path")
        if not target_path:
            logger.warning("No target path provided to get_signed_url.")
            return None

        # Resolve expiration whether called as expires_in_seconds or expires_in
        expiry = expires_in_seconds or kwargs.get("expires_in", 3600)

        try:
            res = self.client.storage.from_(self.bucket_name).create_signed_url(
                path=target_path,
                expires_in=expiry
            )

            # Handle dictionary and object return variations across supabase-py versions
            if isinstance(res, dict):
                return (
                    res.get("signedURL")
                    or res.get("signedUrl")
                    or res.get("signed_url")
                )
            elif hasattr(res, "signed_url"):
                return getattr(res, "signed_url")
            elif hasattr(res, "signedURL"):
                return getattr(res, "signedURL")

            return str(res)
        except Exception as e:
            logger.error(f"Failed to generate signed URL for {target_path}: {str(e)}")
            return None


supabase_storage = SupabaseStorageService()