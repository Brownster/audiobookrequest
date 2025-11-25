from app.internal.models import TorrentSource, BookMetadata
from datetime import datetime

try:
    source = TorrentSource(
        guid="http://mam/123",
        indexer="MyAnonamouse",
        indexer_id=1,
        title="Test Book",
        size=1000,
        publish_date=datetime.now(),
        info_url="https://www.myanonamouse.net/t/123",
        book_metadata=BookMetadata(),
        indexer_flags=[],
        seeders=10,
        leechers=0
    )
    print("Instantiation successful")
except Exception as e:
    print(f"Instantiation failed: {e}")
