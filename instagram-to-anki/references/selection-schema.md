# Confirmed selection format

Use UTF-8 JSON. Paths may be absolute or relative to the process working directory.

```json
{
  "deck": "Instagram English",
  "entries": [
    {
      "word": "counterintuitive",
      "meaning": "反直觉的",
      "context": "It sounds counterintuitive, but it works.",
      "position": "00:18",
      "source_url": "https://www.instagram.com/reel/example/",
      "media": ["C:/downloads/example.mp4"],
      "tags": ["instagram-to-anki", "reel"]
    }
  ]
}
```

`word` and `meaning` are required. All other entry fields are optional. Use an empty `media` array when the user chooses not to attach media. Never include an unconfirmed candidate.
