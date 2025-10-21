import os
import re
from pathlib import Path
from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)

class EnvManager:
    def __init__(self, env_path: str = None):
        self.env_path = env_path or self._find_env_file()
        
    def _find_env_file(self):
        """Mencari file .env dari current directory hingga root"""
        current_dir = Path.cwd()
        
        for parent in [current_dir] + list(current_dir.parents):
            env_file = parent / '.env'
            if env_file.exists():
                return env_file
                
        # Fallback ke current directory
        return current_dir / '.env'
    
    def read_env_file(self) -> List[str]:
        """Membaca seluruh konten .env file"""
        try:
            if not self.env_path.exists():
                return []
                
            with open(self.env_path, 'r', encoding='utf-8') as f:
                return f.readlines()
        except Exception as e:
            logger.error(f"Error reading .env file: {e}")
            return []
    
    def write_env_file(self, lines: List[str]):
        """Menulis seluruh konten ke .env file"""
        try:
            with open(self.env_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            logger.info(f"Successfully updated .env file at {self.env_path}")
            return True
        except Exception as e:
            logger.error(f"Error writing .env file: {e}")
            return False
    
    def get_gemini_keys(self) -> Dict[str, str]:
        """Mendapatkan semua Gemini API keys dari .env"""
        lines = self.read_env_file()
        keys = {}
        
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and 'GEMINI_API_KEY' in line:
                # Support format: GEMINI_API_KEY_1=value atau GEMINI_API_KEYS=value1,value2
                if 'GEMINI_API_KEYS=' in line:
                    # Format lama: GEMINI_API_KEYS=key1,key2,key3
                    key_value = line.split('=', 1)[1].strip().strip('"').strip("'")
                    key_list = [k.strip() for k in key_value.split(',') if k.strip()]
                    for i, key in enumerate(key_list, 1):
                        keys[f'KEY_{i}'] = key
                else:
                    # Format baru: GEMINI_API_KEY_1=value1, GEMINI_API_KEY_2=value2
                    match = re.match(r'GEMINI_API_KEY_(\w+)=(.+)', line)
                    if match:
                        alias = match.group(1)
                        value = match.group(2).strip().strip('"').strip("'")
                        keys[alias] = value
        
        return keys
    
    def update_gemini_keys(self, keys_config: Dict[str, Dict]):
        """
        Update .env file dengan keys baru
        keys_config: {'KEY_1': {'name': 'Key Primary', 'value': 'key123'}, ...}
        """
        lines = self.read_env_file()
        new_lines = []
        gemini_keys_updated = False
        
        # Process existing lines
        for line in lines:
            line_stripped = line.strip()
            
            # Skip old GEMINI_API_KEYS format
            if line_stripped.startswith('GEMINI_API_KEYS='):
                continue
                
            # Skip individual GEMINI_API_KEY_* lines (we'll rewrite them)
            if line_stripped.startswith('GEMINI_API_KEY_'):
                continue
                
            new_lines.append(line)
        
        # Add new individual key entries
        for alias, config in keys_config.items():
            if config.get('value'):
                new_lines.append(f'GEMINI_API_KEY_{alias}={config["value"]}\n')
        
        return self.write_env_file(new_lines)
    
    def add_single_key(self, alias: str, key_value: str, key_name: str = None):
        """Menambah single key ke .env"""
        # 1. Ambil SEMUA keys yang sudah ada
        existing_keys = self.get_gemini_keys()
        
        # 2. Tambahkan key baru ke dalam dictionary
        existing_keys[alias] = key_value
        
        # 3. Siapkan config dalam format yang diharapkan oleh update_gemini_keys
        #    Pastikan kita menyertakan SEMUA keys (lama dan baru)
        updated_config_dict = {}
        for key_alias, value in existing_keys.items():
            # 'name' tidak terlalu penting di sini karena update_gemini_keys hanya butuh 'value'
            updated_config_dict[key_alias] = {'value': value}
            
        # 4. Tulis ulang file .env dengan data yang sudah lengkap
        return self.update_gemini_keys(updated_config_dict)
    
    def remove_key(self, alias: str):
        """Menghapus key dari .env"""
        lines = self.read_env_file()
        new_lines = []
        
        for line in lines:
            line_stripped = line.strip()
            if line_stripped.startswith(f'GEMINI_API_KEY_{alias}='):
                continue  # Skip line yang akan dihapus
            new_lines.append(line)
        
        return self.write_env_file(new_lines)