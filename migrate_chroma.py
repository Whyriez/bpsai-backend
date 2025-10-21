#!/usr/bin/env python3
"""
Script untuk migrate data dari PersistentClient ke HttpClient
"""
import chromadb
import sys

OLD_PATH = "chroma_data/"
NEW_HOST = "localhost"
NEW_PORT = 8000

def migrate():
    print("🔄 Starting ChromaDB migration...")
    
    # Connect to old embedded database
    try:
        old_client = chromadb.PersistentClient(path=OLD_PATH)
        print(f"✅ Connected to old database at {OLD_PATH}")
    except Exception as e:
        print(f"❌ Failed to connect to old database: {e}")
        return False
    
    # Connect to new HTTP server
    try:
        new_client = chromadb.HttpClient(host=NEW_HOST, port=NEW_PORT)
        heartbeat = new_client.heartbeat()
        print(f"✅ Connected to new ChromaDB server (heartbeat: {heartbeat})")
    except Exception as e:
        print(f"❌ Failed to connect to new server: {e}")
        print("   Make sure ChromaDB service is running: sudo systemctl start chromadb")
        return False
    
    # Migrate each collection
    collections_to_migrate = ['berita_bps', 'document_chunks']
    
    for collection_name in collections_to_migrate:
        print(f"\n📦 Migrating collection: {collection_name}")
        try:
            # Get old collection
            old_col = old_client.get_collection(collection_name)
            
            # Get all data
            data = old_col.get(include=['embeddings', 'documents', 'metadatas'])
            
            if not data['ids']:
                print(f"   ⚠️  Collection {collection_name} is empty, skipping...")
                continue
            
            print(f"   📊 Found {len(data['ids'])} items")
            
            # Create new collection
            new_col = new_client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"}
            )
            
            # Migrate in batches of 1000
            batch_size = 1000
            total = len(data['ids'])
            
            for i in range(0, total, batch_size):
                end = min(i + batch_size, total)
                
                new_col.add(
                    ids=data['ids'][i:end],
                    embeddings=data['embeddings'][i:end] if data['embeddings'] else None,
                    documents=data['documents'][i:end] if data['documents'] else None,
                    metadatas=data['metadatas'][i:end] if data['metadatas'] else None
                )
                
                print(f"   ✅ Migrated {end}/{total} items")
            
            print(f"   ✅ Successfully migrated {collection_name}")
            
        except ValueError as e:
            if "does not exist" in str(e):
                print(f"   ⚠️  Collection {collection_name} does not exist in old database")
            else:
                print(f"   ❌ Error: {e}")
        except Exception as e:
            print(f"   ❌ Failed to migrate {collection_name}: {e}")
            return False
    
    print("\n✅ Migration completed successfully!")
    return True

if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)