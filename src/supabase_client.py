        record = {
            "source_name": article.get("source_name"),
            "source_url": source_url,
            "original_title": article.get("original_title"),
            "original_content": article.get("original_content"),
            
            # Add these three lines:
            "title_en": article.get("title_en"),
            "content_en": article.get("content_en"),
            "event_location_en": article.get("event_location_en"),
            
            "image_url": article.get("image_url"),
            "image_credit": article.get("image_credit"),
            "event_date": article.get("event_date") or None,
            "event_location": article.get("event_location"),
            "status": "pending"
        }
