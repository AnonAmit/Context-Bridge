import sys
import json
import re

def extract_pb_strings(data):
    strings = []
    i = 0
    while i < len(data):
        try:
            key = 0
            shift = 0
            start_i = i
            while True:
                if i >= len(data): return strings
                b = data[i]
                i += 1
                key |= (b & 0x7f) << shift
                if not (b & 0x80): break
                shift += 7
            wire_type = key & 7
            field_num = key >> 3
            
            if wire_type == 0:
                while i < len(data) and (data[i] & 0x80): i += 1
                i += 1
            elif wire_type == 1:
                i += 8
            elif wire_type == 5:
                i += 4
            elif wire_type == 2:
                length = 0
                shift = 0
                while True:
                    if i >= len(data): return strings
                    b = data[i]
                    i += 1
                    length |= (b & 0x7f) << shift
                    if not (b & 0x80): break
                    shift += 7
                if length < 0 or i + length > len(data):
                    i = start_i + 1
                    continue
                
                payload = data[i:i+length]
                i += length
                
                # Check for strings
                if length > 5:
                    try:
                        s = payload.decode('utf-8')
                        # only ascii-ish and spaces to filter noise
                        if all(ord(c) >= 32 or c in '\n\r\t' for c in s) and any(c.isspace() for c in s):
                            strings.append({"field": field_num, "text": s})
                    except UnicodeDecodeError:
                        pass
                    
                    # Also try to parse as nested message
                    nested = extract_pb_strings(payload)
                    strings.extend(nested)
            else:
                i = start_i + 1
        except Exception:
            i += 1
    return strings

data = open(r'C:\Users\sumit\.gemini\antigravity\conversations\348cfe61-124d-440b-abcc-ce08aa4f88bf.pb', 'rb').read()
res = extract_pb_strings(data)

# filter for english looking text
english = [r for r in res if len(r['text']) > 15]
with open('pb_debug.json', 'w', encoding='utf-8') as f:
    json.dump(english[:200], f, indent=2, ensure_ascii=False)
print(f"Extracted {len(english)} strings")
