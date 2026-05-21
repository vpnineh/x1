import os
import sys
import re
import json
import base64
import urllib.parse
import concurrent.futures
import requests
import time
from datetime import datetime, timezone
from collections import defaultdict
from tqdm import tqdm
import geoip2.database

# ================= Configuration =================
INPUT_FILE = 'list.conf'
OUTPUT_DIR = 'sub' # تغییر نام پوشه اصلی به sub
GEO_CITY_PATH = 'GeoLite2-City.mmdb'
GEO_ASN_PATH = 'GeoLite2-ASN.mmdb'
GEO_CITY_URL = "https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/geoip/GeoLite2-City.mmdb"
GEO_ASN_URL = "https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/geoip/GeoLite2-ASN.mmdb"
MAX_WORKERS = 30 

# GitHub Repo Info
GITHUB_USER = "YourUsername" 
GITHUB_REPO = "YourRepoName"

# ================= Security Lock =================
EXPECTED_SECRET = "ZX_PROMPT_ADMIN_2026"

if os.environ.get('ZX_SECRET') != EXPECTED_SECRET:
    print("\n⛔ FATAL ERROR: Unauthorized access. Invalid or missing Secret Key.")
    print("Execution Blocked.\n")
    sys.exit(1)

# ================= Remark Settings =================
REMARK_SUFFIX = "Curated by @ZXprompt"
STARTING_ID = 100 
# =================================================

flag_cache = {}

def get_flag(country_iso_code):
    if not country_iso_code: return "🏳️"
    if country_iso_code in flag_cache: return flag_cache[country_iso_code]
    flag = chr(ord(country_iso_code[0]) + 127397) + chr(ord(country_iso_code[1]) + 127397)
    flag_cache[country_iso_code] = flag
    return flag

def download_geo_db():
    # دانلود دیتابیس شهر و دیتاسنتر (ASN)
    dbs = {GEO_CITY_PATH: GEO_CITY_URL, GEO_ASN_PATH: GEO_ASN_URL}
    for path, url in dbs.items():
        if os.path.exists(path):
            if (time.time() - os.path.getmtime(path)) < 7 * 86400:
                print(f"✅ {path} loaded from cache.")
                continue

        print(f"📥 Downloading fresh {path}...")
        try:
            response = requests.get(url, stream=True)
            total_size = int(response.headers.get('content-length', 0))
            with open(path, 'wb') as file, tqdm(
                desc=f"GEO DB ({path[:10]})", total=total_size, unit='iB', unit_scale=True, unit_divisor=1024
            ) as bar:
                for data in response.iter_content(chunk_size=1024):
                    size = file.write(data)
                    bar.update(size)
        except Exception as e:
            print(f"❌ Failed to download {path}: {e}")
            sys.exit(1)

def safe_b64decode(data):
    try:
        data = re.sub(r'\s+', '', data)
        return base64.b64decode(data + '=' * (-len(data) % 4)).decode('utf-8')
    except:
        return ""

def fetch_sub_links(url):
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            text = resp.text
            if '://' not in text[:50]:
                text = safe_b64decode(text)
            return [line.strip() for line in text.splitlines() if '://' in line]
    except:
        pass
    return []

def get_geo(host, city_reader, asn_reader):
    if not host: return {'country': 'Unknown', 'flag': '🏳️', 'datacenter': 'Unknown'}
    try:
        ip = host
        if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host):
             import socket
             ip = socket.gethostbyname(host)
             
        # دریافت لوکیشن
        try:
            city_res = city_reader.city(ip)
            country_name = city_res.country.name if city_res.country.name else 'Unknown'
            iso_code = city_res.country.iso_code
        except:
            country_name, iso_code = 'Unknown', None
            
        # دریافت دیتاسنتر
        try:
            asn_res = asn_reader.asn(ip)
            datacenter = asn_res.autonomous_system_organization if asn_res.autonomous_system_organization else 'Unknown'
        except:
            datacenter = 'Unknown'
            
        return {
            'country': country_name.replace(' ', '_'), 
            'flag': get_flag(iso_code),
            'datacenter': re.sub(r'[^A-Za-z0-9_]', '', datacenter) # پاکسازی نام دیتاسنتر برای فولدر
        }
    except:
        return {'country': 'Unknown', 'flag': '🏳️', 'datacenter': 'Unknown'}

def process_config(link, city_reader, asn_reader):
    if not link: return None
    try:
        if link.startswith('vmess://'):
            b64_str = link[8:]
            decoded = safe_b64decode(b64_str)
            if not decoded: return None
            
            conf = json.loads(decoded)
            host = conf.get('add', '')
            port = conf.get('port', '')
            uid = conf.get('id', '')
            
            dedup_key = f"vmess_{host}_{port}_{uid}"
            geo = get_geo(host, city_reader, asn_reader)
            
            return {'raw': conf, 'type': 'vmess', 'type_name': 'Vmess', 'dedup': dedup_key, 'geo': geo, 'is_vmess': True}
        else:
            parsed = urllib.parse.urlparse(link)
            scheme = parsed.scheme
            netloc = parsed.netloc 
            
            if '@' in netloc:
                auth, host_port = netloc.split('@', 1)
            else:
                host_port = netloc
                auth = ""
                
            if ':' in host_port:
                host, port = host_port.split(':', 1)
            else:
                host = host_port
                port = "443"
                
            dedup_key = f"{scheme}_{host}_{port}_{auth}"
            geo = get_geo(host, city_reader, asn_reader)
            
            return {'raw': link, 'parsed': parsed, 'netloc': netloc, 'type': scheme, 'type_name': scheme.capitalize(), 'dedup': dedup_key, 'geo': geo, 'is_vmess': False}
    except:
        return None

def format_remark(uid, conf_data):
    geo = conf_data['geo']
    base_remark = f"{uid} - {conf_data['type_name']} - {geo['flag']} {geo['country']} | {REMARK_SUFFIX}"
    
    if conf_data['is_vmess']:
        conf = conf_data['raw']
        conf['ps'] = base_remark
        return "vmess://" + base64.b64encode(json.dumps(conf, separators=(',', ':')).encode('utf-8')).decode('utf-8')
    else:
        parsed = conf_data['parsed']
        netloc = conf_data['netloc']
        scheme = conf_data['type']
        new_remark = urllib.parse.quote(base_remark)
        return f"{scheme}://{netloc}?{parsed.query}#{new_remark}"

def save_to_file(filepath, lines, to_base64=False):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    content = '\n'.join(lines)
    if to_base64:
        content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

def generate_readme(total_configs, by_protocol):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    github_repo_env = os.environ.get('GITHUB_REPOSITORY', f'{GITHUB_USER}/{GITHUB_REPO}')
    base_raw_url = f"https://raw.githubusercontent.com/{github_repo_env}/main/{OUTPUT_DIR}"
    
    stats_md = "\n".join([f"- **{proto.capitalize()}**: {len(links)}" for proto, links in by_protocol.items()])
    
    eng_text = f"""
## 🇬🇧 Free VPN Subscriptions
> Automatically fetched, deeply deduplicated, and categorized based on GeoIP.
> **Last Update:** `{now}`
> **Total Active Configs:** `{total_configs}`

### 📊 Statistics
{stats_md}

### 🔗 Main Links (Import these to your client)
* **Mix All (Base64):** `{base_raw_url}/base64/all`
* **Mix All (Normal):** `{base_raw_url}/normal/all`
"""
    readme_content = f"# 🚀 ZX Auto Processor Subscriptions\n\n{eng_text}\n\n---\n*Powered by GitHub Actions & Python*"
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme_content)

def main():
    print(f"🔒 Security Check Passed. Authenticated as: {REMARK_SUFFIX}")
    
    if not os.path.exists(INPUT_FILE):
        print(f"❌ Error: {INPUT_FILE} not found.")
        return

    download_geo_db()

    with open(INPUT_FILE, 'r') as f:
        sub_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    raw_links = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_sub_links, url): url for url in sub_urls}
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="🌐 Fetching Subs ", unit="sub"):
            raw_links.extend(future.result())
    
    unique_configs = {}
    valid_configs = []
    city_reader = geoip2.database.Reader(GEO_CITY_PATH)
    asn_reader = geoip2.database.Reader(GEO_ASN_PATH)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_config, link, city_reader, asn_reader) for link in raw_links]
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="⚙️  Processing  ", unit="link"):
            data = future.result()
            if data and data['dedup'] not in unique_configs:
                unique_configs[data['dedup']] = True
                valid_configs.append(data)
                
    city_reader.close()
    asn_reader.close()

    by_country = defaultdict(list)
    by_protocol = defaultdict(list)
    by_datacenter = defaultdict(list)
    all_links = []
    current_id = STARTING_ID

    for conf in tqdm(valid_configs, desc="✍️  Applying IDs ", unit="conf"):
        final_link = format_remark(current_id, conf)
        country = conf['geo']['country']
        protocol = conf['type']
        datacenter = conf['geo']['datacenter']

        all_links.append(final_link)
        by_country[country].append(final_link)
        by_protocol[protocol].append(final_link)
        if datacenter != 'Unknown':
            by_datacenter[datacenter].append(final_link)
            
        current_id += 1

    print("📁 Saving categorized output files...")
    
    # 1. فایل‌های all (بدون پسوند)
    save_to_file(f"{OUTPUT_DIR}/normal/all", all_links)
    save_to_file(f"{OUTPUT_DIR}/base64/all", all_links, to_base64=True)

    # 2. تفکیک لوکیشن
    for country, links in by_country.items():
        save_to_file(f"{OUTPUT_DIR}/normal/Location/{country}", links)
        save_to_file(f"{OUTPUT_DIR}/base64/Location/{country}", links, to_base64=True)

    # 3. تفکیک دیتاسنتر
    for dc, links in by_datacenter.items():
        if dc: # جلوگیری از نام خالی
            save_to_file(f"{OUTPUT_DIR}/normal/Datacenter/{dc}", links)
            save_to_file(f"{OUTPUT_DIR}/base64/Datacenter/{dc}", links, to_base64=True)

    # 4. تفکیک پروتکل
    for proto, links in by_protocol.items():
        save_to_file(f"{OUTPUT_DIR}/normal/Protocol/{proto}", links)
        save_to_file(f"{OUTPUT_DIR}/base64/Protocol/{proto}", links, to_base64=True)
        
    generate_readme(len(valid_configs), by_protocol)
    print("🚀 All processes completed successfully! Ready for commit.")

if __name__ == '__main__':
    main()
