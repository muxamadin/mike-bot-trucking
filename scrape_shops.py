"""
Scrapes truck repair shop locations from @Ezwaysbot
by sending GPS coordinates for major cities in all 48 states.
Run: python3 scrape_shops.py
"""

import asyncio
import json
import re
import time
from telethon import TelegramClient
from telethon.tl.types import InputGeoPoint
from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.types import InputMediaGeoPoint

API_ID = "35456513"
API_HASH = "bc42fbf9f7a48c6ccfd58c151948e0b0"

# Major city coordinates for all 48 contiguous states
CITIES = [
    ("Alabama - Montgomery",        32.3792,  -86.3077),
    ("Arizona - Phoenix",           33.4484, -112.0740),
    ("Arkansas - Little Rock",      34.7465,  -92.2896),
    ("California - Los Angeles",    34.0522, -118.2437),
    ("California - San Francisco",  37.7749, -122.4194),
    ("California - Fresno",         36.7378, -119.7871),
    ("Colorado - Denver",           39.7392, -104.9903),
    ("Connecticut - Hartford",      41.7658,  -72.6851),
    ("Delaware - Dover",            39.1582,  -75.5244),
    ("Florida - Miami",             25.7617,  -80.1918),
    ("Florida - Jacksonville",      30.3322,  -81.6557),
    ("Florida - Tampa",             27.9506,  -82.4572),
    ("Georgia - Atlanta",           33.7490,  -84.3880),
    ("Idaho - Boise",               43.6150, -116.2023),
    ("Illinois - Chicago",          41.8781,  -87.6298),
    ("Illinois - Springfield",      39.7817,  -89.6501),
    ("Indiana - Indianapolis",      39.7684,  -86.1581),
    ("Iowa - Des Moines",           41.5868,  -93.6250),
    ("Kansas - Wichita",            37.6872,  -97.3301),
    ("Kansas - Kansas City",        39.1141,  -94.6275),
    ("Kentucky - Louisville",       38.2527,  -85.7585),
    ("Louisiana - New Orleans",     29.9511,  -90.0715),
    ("Louisiana - Baton Rouge",     30.4515,  -91.1871),
    ("Maine - Portland",            43.6591,  -70.2568),
    ("Maryland - Baltimore",        39.2904,  -76.6122),
    ("Massachusetts - Boston",      42.3601,  -71.0589),
    ("Michigan - Detroit",          42.3314,  -83.0458),
    ("Michigan - Grand Rapids",     42.9634,  -85.6681),
    ("Minnesota - Minneapolis",     44.9778,  -93.2650),
    ("Mississippi - Jackson",       32.2988,  -90.1848),
    ("Missouri - St Louis",         38.6270,  -90.1994),
    ("Missouri - Kansas City MO",   39.0997,  -94.5786),
    ("Montana - Billings",          45.7833, -108.5007),
    ("Nebraska - Omaha",            41.2565,  -95.9345),
    ("Nevada - Las Vegas",          36.1699, -115.1398),
    ("Nevada - Reno",               39.5296, -119.8138),
    ("New Hampshire - Manchester",  42.9956,  -71.4548),
    ("New Jersey - Newark",         40.7357,  -74.1724),
    ("New Mexico - Albuquerque",    35.0844, -106.6504),
    ("New York - New York City",    40.7128,  -74.0060),
    ("New York - Buffalo",          42.8864,  -78.8784),
    ("North Carolina - Charlotte",  35.2271,  -80.8431),
    ("North Carolina - Raleigh",    35.7796,  -78.6382),
    ("North Dakota - Fargo",        46.8772,  -96.7898),
    ("Ohio - Columbus",             39.9612,  -82.9988),
    ("Ohio - Cleveland",            41.4993,  -81.6944),
    ("Oklahoma - Oklahoma City",    35.4676,  -97.5164),
    ("Oregon - Portland",           45.5051, -122.6750),
    ("Pennsylvania - Philadelphia", 39.9526,  -75.1652),
    ("Pennsylvania - Pittsburgh",   40.4406,  -79.9959),
    ("Rhode Island - Providence",   41.8240,  -71.4128),
    ("South Carolina - Columbia",   34.0007,  -81.0348),
    ("South Dakota - Sioux Falls",  43.5446,  -96.7311),
    ("Tennessee - Nashville",       36.1627,  -86.7816),
    ("Tennessee - Memphis",         35.1495,  -90.0490),
    ("Texas - Dallas",              32.7767,  -96.7970),
    ("Texas - Houston",             29.7604,  -95.3698),
    ("Texas - San Antonio",         29.4241,  -98.4936),
    ("Texas - El Paso",             31.7619, -106.4850),
    ("Utah - Salt Lake City",       40.7608, -111.8910),
    ("Vermont - Burlington",        44.4759,  -73.2121),
    ("Virginia - Richmond",         37.5407,  -77.4360),
    ("Virginia - Virginia Beach",   36.8529,  -75.9780),
    ("Washington - Seattle",        47.6062, -122.3321),
    ("Washington - Spokane",        47.6588, -117.4260),
    ("West Virginia - Charleston",  38.3498,  -81.6326),
    ("Wisconsin - Milwaukee",       43.0389,  -88.0063),
    ("Wyoming - Cheyenne",          41.1400, -104.8202),
]

all_shops = {}

async def main():
    client = TelegramClient('session', API_ID, API_HASH)
    await client.start(phone="+12194443285", code_callback=lambda: "64131")
    print("✅ Connected to Telegram!")

    # Send to the UHF-ALLSERVICES group where Ezwaysbot is active (group ID -2653025117)
    from telethon.tl.types import PeerChannel
    group = await client.get_entity(PeerChannel(2653025117))
    print(f"✅ Found group: {group.title}")

    for city_name, lat, lon in CITIES:
        print(f"\n📍 Querying: {city_name}")
        try:
            # Note last message ID before sending
            before_msgs = await client.get_messages(group, limit=1)
            last_id = before_msgs[0].id if before_msgs else 0

            # Send location to the group
            await client(SendMediaRequest(
                peer=group,
                media=InputMediaGeoPoint(
                    geo_point=InputGeoPoint(lat=lat, long=lon)
                ),
                message=""
            ))

            # Wait for bot to respond (sends up to 3 parts)
            await asyncio.sleep(15)

            # Get new messages after our location
            new_messages = await client.get_messages(group, limit=10, min_id=last_id)
            shop_text = ""
            for msg in reversed(new_messages):
                if msg.text and len(msg.text) > 50:
                    shop_text += msg.text + "\n---\n"
                    print(f"   MSG: {msg.text[:100]}")

            if shop_text:
                all_shops[city_name] = shop_text
                print(f"   ✅ Got data for {city_name}")
            else:
                print(f"   ⚠️ No shops found near {city_name}")

            # Wait between queries
            await asyncio.sleep(10)

        except Exception as e:
            print(f"   ❌ Error for {city_name}: {e}")
            await asyncio.sleep(10)

    # Save all results
    with open("shop_locations.json", "w", encoding="utf-8") as f:
        json.dump(all_shops, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Done! Saved {len(all_shops)} city results to shop_locations.json")
    await client.disconnect()

asyncio.run(main())
