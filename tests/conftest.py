import os
import shutil
from pathlib import Path
from argon2 import PasswordHasher

TEST_STATE = Path(__file__).parent / ".runtime"
shutil.rmtree(TEST_STATE, ignore_errors=True)
os.environ["MINIPACS_STATE_ROOT"] = str(TEST_STATE)
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD_HASH"] = PasswordHasher().hash("correct horse battery staple")
os.environ["SESSION_SECRET"] = "test-secret" * 4
os.environ["COOKIE_SECURE"] = "false"
os.environ["ALLOWED_CALLING_AE"] = "TESTSCU"
