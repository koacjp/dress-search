import sys
import os
import re
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from flask import Flask, request, jsonify, render_template
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def natural_language_to_query(user_input):
    """自然言語をフリマ検索キーワード+除外ワード+価格帯に変換する"""
    if not ANTHROPIC_API_KEY:
        return {"keywords": user_input, "exclude": [], "exclude_color": "", "min_price": None, "max_price": None}

    prompt = f"""あなたはフリマサイトの検索クエリ最適化AIです。
ユーザーの自然言語の入力を、フリマサイト（ヤフオク・PayPayフリマ・ラクマ）で最も良い結果が出る検索キーワードに変換してください。

ルール：
- keywordsは日本語のフリマ検索に最適化されたスペース区切りのキーワード（3-5語）
- excludeはタイトルに含まれていたら除外すべきワードのリスト
- exclude_colorは除外したい色（「黒」「白」等）。なければ空文字
- min_price/max_priceは数値。指定がなければnull

例1:
入力: 「結婚式の二次会に着ていけるドレス。上品だけどキャバ嬢っぽくない。黒はダメ」
出力: {{"keywords": "パーティードレス 上品 レース ワンピース", "exclude": ["コスプレ", "子供", "キッズ", "ベビー", "キャバ", "セクシー", "ミニ丈", "カード"], "exclude_color": "黒", "min_price": null, "max_price": null}}

例2:
入力: 「3000円から7000円くらいで上品なドレス」
出力: {{"keywords": "ドレス フォーマル 上品 レース", "exclude": ["コスプレ", "子供", "キッズ", "カード"], "exclude_color": "", "min_price": 3000, "max_price": 7000}}

入力: 「{user_input}」
出力（JSONのみ、説明不要）:"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=10
        )
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"].strip()
            # JSONだけ抽出
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        print(f"[AI] Error: {e}")

    return {"keywords": user_input, "exclude": [], "exclude_color": "", "min_price": None, "max_price": None}

# User-Agent設定
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
UA_PC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HEADERS_PC = {"User-Agent": UA_PC, "Accept-Language": "ja-JP,ja;q=0.9"}
HEADERS_MOBILE = {"User-Agent": UA_MOBILE, "Accept-Language": "ja-JP,ja;q=0.9"}


def extract_next_data(html):
    """HTMLから__NEXT_DATA__のJSONを抽出する"""
    match = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.*?})\s*</script>', html, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return None


def extract_original_price(text):
    """説明文から定価・元値・参考価格を抽出する"""
    if not text:
        return None
    patterns = [
        r'定価[：:\s]*([0-9,]+)',
        r'元値[：:\s]*([0-9,]+)',
        r'参考価格[：:\s]*([0-9,]+)',
        r'正規価格[：:\s]*([0-9,]+)',
        r'購入価格[：:\s]*([0-9,]+)',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return f"定価{m.group(1)}円"
    return None


def should_exclude(title, exclude_color):
    """除外色フィルター"""
    if not exclude_color:
        return False
    color_map = {
        "黒": ["黒", "ブラック", "black"],
        "白": ["白", "ホワイト", "white"],
        "赤": ["赤", "レッド", "red"],
        "青": ["青", "ブルー", "blue"],
        "ピンク": ["ピンク", "pink"],
    }
    keywords = color_map.get(exclude_color, [exclude_color])
    title_lower = title.lower()
    return any(k.lower() in title_lower for k in keywords)


def search_mercari(keyword, price_min, price_max, exclude_color):
    """メルカリ検索"""
    results = []
    try:
        encoded = urllib.parse.quote(keyword)
        url = f"https://jp.mercari.com/search?keyword={encoded}&price_min={price_min}&price_max={price_max}&status=on_sale"
        resp = requests.get(url, headers=HEADERS_PC, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # __NEXT_DATA__から取得を試みる
        next_data = extract_next_data(html)
        if next_data:
            try:
                # pagePropsの中の商品データを探す
                page_props = next_data.get("props", {}).get("pageProps", {})
                # 検索結果のキーを探す
                items = []
                for key in ["search", "searchResult", "data", "items", "results"]:
                    if key in page_props:
                        data = page_props[key]
                        if isinstance(data, list):
                            items = data
                            break
                        elif isinstance(data, dict):
                            for subkey in ["items", "results", "products", "data"]:
                                if subkey in data and isinstance(data[subkey], list):
                                    items = data[subkey]
                                    break
                        if items:
                            break

                for item in items:
                    title = item.get("name", item.get("title", item.get("productName", "")))
                    if should_exclude(title, exclude_color):
                        continue
                    price = item.get("price", item.get("currentPrice", 0))
                    if isinstance(price, str):
                        price = int(re.sub(r'[^\d]', '', price) or 0)
                    image = item.get("thumbnails", item.get("imageURL", item.get("thumbnail", "")))
                    if isinstance(image, list) and image:
                        image = image[0]
                    item_id = item.get("id", item.get("productId", ""))
                    item_url = f"https://jp.mercari.com/item/{item_id}" if item_id else url
                    desc = item.get("description", "")
                    original = extract_original_price(desc)

                    if title and image:
                        results.append({
                            "title": title,
                            "price": int(price) if price else 0,
                            "image": image if image.startswith("https://") else "",
                            "url": item_url,
                            "source": "mercari",
                            "original_price": original,
                            "shipping": "送料込み"
                        })
            except Exception:
                pass

        # __NEXT_DATA__から取れなかった場合、HTMLパースにフォールバック
        if not results:
            soup = BeautifulSoup(html, 'html.parser')
            # メルカリの商品カードを探す
            items = soup.select('[data-testid="item-cell"]') or soup.select('li[data-testid]') or soup.select('.sc-bcd5623a-0')
            if not items:
                # より広いセレクタで探す
                items = soup.find_all('a', href=re.compile(r'/item/m\d+'))

            for item in items[:30]:
                try:
                    link = item if item.name == 'a' else item.find('a', href=re.compile(r'/item/'))
                    if not link:
                        continue
                    href = link.get('href', '')
                    item_url = f"https://jp.mercari.com{href}" if href.startswith('/') else href

                    img = item.find('img')
                    image = img.get('src', '') if img else ''
                    if not image.startswith('https://'):
                        image = img.get('data-src', '') if img else ''

                    # タイトル
                    title_el = item.find('span', class_=re.compile(r'itemName|title')) or (img and img.get('alt'))
                    if isinstance(title_el, str):
                        title = title_el
                    elif title_el:
                        title = title_el.get_text(strip=True)
                    else:
                        title = img.get('alt', '') if img else ''

                    if should_exclude(title, exclude_color):
                        continue

                    # 価格
                    price_el = item.find('span', class_=re.compile(r'price|Price'))
                    price_text = price_el.get_text(strip=True) if price_el else '0'
                    price = int(re.sub(r'[^\d]', '', price_text) or 0)

                    if title and image.startswith('https://'):
                        results.append({
                            "title": title,
                            "price": price,
                            "image": image,
                            "url": item_url,
                            "source": "mercari",
                            "original_price": None,
                            "shipping": "送料込み"
                        })
                except Exception:
                    continue

    except Exception as e:
        print(f"[mercari] Error: {e}")
    return results


def search_paypay(keyword, price_min, price_max, exclude_color):
    """PayPayフリマ検索"""
    results = []
    try:
        encoded = urllib.parse.quote(keyword)
        url = f"https://paypayfleamarket.yahoo.co.jp/search/{encoded}?sort=price_asc&price_min={price_min}&price_max={price_max}"
        resp = requests.get(url, headers=HEADERS_MOBILE, timeout=15)
        resp.raise_for_status()
        html = resp.text

        next_data = extract_next_data(html)
        if next_data:
            try:
                page_props = next_data.get("props", {}).get("pageProps", {})
                # 検索結果を探す
                items = []
                for key in ["searchResult", "search", "data", "items", "results"]:
                    if key in page_props:
                        data = page_props[key]
                        if isinstance(data, list):
                            items = data
                            break
                        elif isinstance(data, dict):
                            for subkey in ["items", "results", "products", "data", "list"]:
                                if subkey in data and isinstance(data[subkey], list):
                                    items = data[subkey]
                                    break
                        if items:
                            break

                # dehydratedStateからも探す
                if not items:
                    dehydrated = page_props.get("dehydratedState", {})
                    queries = dehydrated.get("queries", [])
                    for q in queries:
                        state = q.get("state", {})
                        data = state.get("data", {})
                        if isinstance(data, dict):
                            for subkey in ["items", "results", "products", "list"]:
                                if subkey in data and isinstance(data[subkey], list):
                                    items = data[subkey]
                                    break
                        elif isinstance(data, list):
                            items = data
                        if items:
                            break

                for item in items:
                    title = item.get("title", item.get("name", item.get("productName", "")))
                    if should_exclude(title, exclude_color):
                        continue
                    price = item.get("price", item.get("currentPrice", 0))
                    if isinstance(price, str):
                        price = int(re.sub(r'[^\d]', '', price) or 0)
                    image = item.get("imageUrl", item.get("image", item.get("thumbnailUrl", "")))
                    if isinstance(image, list) and image:
                        image = image[0]
                    item_id = item.get("id", item.get("itemId", item.get("productId", "")))
                    item_url = f"https://paypayfleamarket.yahoo.co.jp/item/{item_id}" if item_id else url
                    desc = item.get("description", "")
                    original = extract_original_price(desc)
                    shipping_text = "送料込み" if item.get("shippingIncluded", True) else "着払い"

                    if title:
                        results.append({
                            "title": title,
                            "price": int(price) if price else 0,
                            "image": image if isinstance(image, str) and image.startswith("https://") else "",
                            "url": item_url,
                            "source": "paypay",
                            "original_price": original,
                            "shipping": shipping_text
                        })
            except Exception:
                pass

        # フォールバック: HTMLパース
        if not results:
            soup = BeautifulSoup(html, 'html.parser')
            items = soup.select('[class*="ItemCard"]') or soup.select('[class*="item"]') or soup.find_all('a', href=re.compile(r'/item/'))
            for item in items[:30]:
                try:
                    link = item if item.name == 'a' else item.find('a', href=re.compile(r'/item/'))
                    if not link:
                        continue
                    href = link.get('href', '')
                    item_url = f"https://paypayfleamarket.yahoo.co.jp{href}" if href.startswith('/') else href

                    img = item.find('img')
                    image = img.get('src', '') if img else ''
                    title = img.get('alt', '') if img else ''

                    if should_exclude(title, exclude_color):
                        continue

                    price_el = item.find(string=re.compile(r'[0-9,]+円'))
                    price_text = price_el.strip() if price_el else '0'
                    price = int(re.sub(r'[^\d]', '', price_text) or 0)

                    if title and image.startswith('https://'):
                        results.append({
                            "title": title,
                            "price": price,
                            "image": image,
                            "url": item_url,
                            "source": "paypay",
                            "original_price": None,
                            "shipping": "送料込み"
                        })
                except Exception:
                    continue

    except Exception as e:
        print(f"[paypay] Error: {e}")
    return results


def search_yahoo_auction(keyword, price_min, price_max, exclude_color):
    """ヤフオク検索"""
    results = []
    try:
        encoded = urllib.parse.quote(keyword)
        url = f"https://auctions.yahoo.co.jp/search/search?p={encoded}&min={price_min}&max={price_max}&istatus=0&exflg=1&b=1&n=40&s1=cbids&o1=a"
        resp = requests.get(url, headers=HEADERS_PC, timeout=15)
        resp.raise_for_status()
        html = resp.text

        soup = BeautifulSoup(html, 'html.parser')

        # ヤフオクの商品リストを取得（複数のセレクタを試す）
        items = soup.select('.Product') or soup.select('li.Product') or soup.find_all('div', class_=re.compile(r'Product'))

        for item in items[:30]:
            try:
                # リンク取得
                link = item.find('a', href=re.compile(r'auctions\.yahoo\.co\.jp'))
                if not link:
                    continue
                item_url = link.get('href', '')

                # 画像取得
                img = item.find('img')
                image = ''
                if img:
                    image = img.get('src', '') or img.get('data-src', '') or img.get('data-original', '')
                    if image and not image.startswith('https://'):
                        image = 'https:' + image if image.startswith('//') else ''

                # タイトル取得
                title_el = item.find('h3') or item.find(class_=re.compile(r'Product__title'))
                if title_el:
                    title = title_el.get_text(strip=True)
                elif img:
                    title = img.get('alt', '')
                else:
                    title = link.get_text(strip=True) if link else ''

                if not title or should_exclude(title, exclude_color):
                    continue

                # 価格取得（複数のパターンを試す）
                price = 0
                # パターン1: Product__priceValue
                price_el = item.find(class_=re.compile(r'Product__priceValue'))
                if price_el:
                    price_text = price_el.get_text(strip=True)
                    digits = re.sub(r'[^\d]', '', price_text)
                    if digits:
                        price = int(digits)

                # パターン2: data-auction-price属性
                if price == 0:
                    price_attr = item.get('data-auction-price') or item.find(attrs={'data-auction-price': True})
                    if price_attr:
                        if isinstance(price_attr, str):
                            price = int(re.sub(r'[^\d]', '', price_attr) or 0)
                        else:
                            price = int(re.sub(r'[^\d]', '', price_attr.get('data-auction-price', '0')) or 0)

                # パターン3: 価格っぽいspanを全部探す
                if price == 0:
                    for span in item.find_all('span'):
                        text = span.get_text(strip=True)
                        if re.match(r'^[\d,]+円?$', text):
                            digits = re.sub(r'[^\d]', '', text)
                            if digits and 100 <= int(digits) <= 999999:
                                price = int(digits)
                                break

                # パターン4: item全体のテキストから価格抽出
                if price == 0:
                    item_text = item.get_text()
                    price_match = re.search(r'([\d,]{3,7})\s*円', item_text)
                    if price_match:
                        price = int(price_match.group(1).replace(',', ''))

                results.append({
                    "title": title,
                    "price": price,
                    "image": image if image.startswith('https://') else "",
                    "url": item_url,
                    "source": "yahoo",
                    "original_price": None,
                    "shipping": ""
                })
            except Exception as e:
                print(f"[yahoo] item parse error: {e}")
                continue

    except Exception as e:
        print(f"[yahoo] Error: {e}")
    return results


def search_rakuma(keyword, price_min, price_max, exclude_color):
    """ラクマ検索"""
    results = []
    try:
        encoded = urllib.parse.quote(keyword)
        url = f"https://fril.jp/search/{encoded}?min={price_min}&max={price_max}&order=asc&sort=sell_price&transaction=selling"
        resp = requests.get(url, headers=HEADERS_PC, timeout=15)
        resp.raise_for_status()
        html = resp.text

        soup = BeautifulSoup(html, 'html.parser')

        # ラクマの商品カード
        items = soup.select('.item-box') or soup.select('[class*="item"]') or soup.find_all('div', class_=re.compile(r'item'))

        for item in items[:30]:
            try:
                link = item.find('a', href=re.compile(r'/item/'))
                if not link:
                    continue
                href = link.get('href', '')
                item_url = f"https://fril.jp{href}" if href.startswith('/') else href

                img = item.find('img')
                image = ''
                if img:
                    image = img.get('src', '') or img.get('data-src', '') or img.get('data-original', '')
                    if image and not image.startswith('https://'):
                        image = 'https:' + image if image.startswith('//') else ''

                title = ''
                if img:
                    title = img.get('alt', '')
                title_el = item.find(class_=re.compile(r'item-name|item_name|title'))
                if title_el:
                    title = title_el.get_text(strip=True)

                if not title or should_exclude(title, exclude_color):
                    continue

                price = 0
                price_el = item.find(class_=re.compile(r'item-price|item_price|price'))
                if price_el:
                    digits = re.sub(r'[^\d]', '', price_el.get_text(strip=True))
                    if digits:
                        price = int(digits)

                if price == 0:
                    for span in item.find_all('span'):
                        text = span.get_text(strip=True)
                        if '¥' in text or '円' in text:
                            digits = re.sub(r'[^\d]', '', text)
                            if digits and 100 <= int(digits) <= 999999:
                                price = int(digits)
                                break

                results.append({
                    "title": title,
                    "price": price,
                    "image": image if image.startswith('https://') else "",
                    "url": item_url,
                    "source": "rakuma",
                    "original_price": None,
                    "shipping": ""
                })
            except Exception:
                continue

    except Exception as e:
        print(f"[rakuma] Error: {e}")
    return results


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/search')
def search():
    raw_query = request.args.get('q', 'パーティードレス レース')
    price_min = request.args.get('min', '3000')
    price_max = request.args.get('max', '7000')
    exclude_color = request.args.get('exclude_color', '')

    # 自然言語をAIで検索クエリに変換
    ai_result = natural_language_to_query(raw_query)
    keyword = ai_result.get("keywords", raw_query)
    ai_excludes = ai_result.get("exclude", [])
    if ai_result.get("exclude_color"):
        exclude_color = ai_result["exclude_color"]
    if ai_result.get("min_price"):
        price_min = str(ai_result["min_price"])
    if ai_result.get("max_price"):
        price_max = str(ai_result["max_price"])

    print(f"[AI] '{raw_query}' → keywords='{keyword}', exclude={ai_excludes}, color={exclude_color}")

    all_results = []

    # 並列で3サイト検索（メルカリ除外、ラクマ追加）
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(search_paypay, keyword, price_min, price_max, exclude_color): "paypay",
            executor.submit(search_yahoo_auction, keyword, price_min, price_max, exclude_color): "yahoo",
            executor.submit(search_rakuma, keyword, price_min, price_max, exclude_color): "rakuma",
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
                print(f"[{source}] {len(results)} items found")
            except Exception as e:
                print(f"[{source}] Error: {e}")

    # AI除外ワードでフィルタリング
    if ai_excludes:
        filtered = []
        for r in all_results:
            title_lower = r["title"].lower()
            if not any(ex.lower() in title_lower for ex in ai_excludes):
                filtered.append(r)
        print(f"[AI filter] {len(all_results)} → {len(filtered)} items (excluded {len(all_results)-len(filtered)})")
        all_results = filtered

    # 価格でソート（0は最後に）
    all_results.sort(key=lambda x: (x["price"] == 0, x["price"]))

    return jsonify({
        "results": all_results,
        "parsed": {
            "original": raw_query,
            "keywords": keyword,
            "exclude": ai_excludes,
            "exclude_color": exclude_color,
            "price_min": price_min,
            "price_max": price_max
        }
    })


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('RENDER') is None  # ローカルのみdebug
    print("=== Dress Search Server ===")
    print(f"http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
