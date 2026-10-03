# Confirmed selection format

Use UTF-8 JSON. Paths may be absolute or relative to the process working directory.

```json
{
  "deck": "ins",
  "entries": [
    {
      "word": "counterintuitive",
      "meaning": "adj.\n/ˌkaʊntərɪnˈtuːɪtɪv/\n反直觉的；与直觉相悖的\n用法：常修饰 result / idea / approach，强调结果出乎意料却可能成立",
      "context": "It sounds counterintuitive, but it works.",
      "position": "00:18",
      "source_url": "https://www.instagram.com/reel/example/",
      "media": ["C:/downloads/example.mp4"],
      "pronunciation": "C:/downloads/ig2anki_counterintuitive.mp3",
      "tags": ["instagram-to-anki", "reel"]
    }
  ]
}
```

`word` and `meaning` are required. All other entry fields are optional. Use an empty `media` array when the user chooses not to attach media. Omit `pronunciation` when the user declines it. Never include an unconfirmed candidate.
