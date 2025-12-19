import os
from dotenv import load_dotenv

load_dotenv()

print("=== DEBUG API KEYS ===")

# Cek semua environment variables yang berhubungan dengan Gemini
gemini_vars = [var for var in os.environ if 'GEMINI' in var.upper()]
for var in gemini_vars:
    value = os.getenv(var)
    if value:
        print(f"{var}: {'*' * 8}{value[-6:]}")
    else:
        print(f"{var}: NOT SET")

# Cek format spesifik
print("\n=== CHECKING SPECIFIC FORMATS ===")

# Format baru
for i in range(1, 10):
    key_name = f"GEMINI_API_KEY_{i}"
    value = os.getenv(key_name)
    if value:
        print(f"✓ {key_name}: Present")
    else:
        print(f"✗ {key_name}: Not found")

# Format lama
old_format = os.getenv('GEMINI_API_KEYS')
if old_format:
    print(f"✓ GEMINI_API_KEYS: Present")
    keys = [k.strip() for k in old_format.split(',') if k.strip()]
    print(f"  Found {len(keys)} keys in old format")
else:
    print("✗ GEMINI_API_KEYS: Not found")

print("\n=== CURRENT WORKING DIRECTORY ===")
print(f"Working dir: {os.getcwd()}")
print(f"Env file: {os.path.join(os.getcwd(), '.env')}")