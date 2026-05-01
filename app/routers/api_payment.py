from fastapi import APIRouter, HTTPException, Query
import httpx
from typing import Optional

router = APIRouter(prefix="/api/payment", tags=["payment-api"])

# BINTable API Configuration
# Для получения API ключа: https://bintable.com/
# Бесплатный план: 100 lookup/месяц
# Если ключ не указан, используется fallback определение схемы карты по номеру
BINTABLE_API_URL = "https://api.bintable.com/v1"
# В production лучше вынести в config.py или environment variables
BINTABLE_API_KEY = "acb605ffc7f764ffb8bb7539d1ffdea48b22a7db"  # API ключ от BINTable


@router.get("/bin-lookup")
async def bin_lookup(
    bin: str = Query(..., description="Первые 6 цифр номера карты (BIN)"),
    api_key: Optional[str] = Query(None, description="API ключ BINTable (опционально)")
):
    """
    Прокси для BINTable API - определяет банк и тип карты по BIN.
    Используется для динамического отображения стиля карты.
    """
    if not bin or len(bin) < 6:
        raise HTTPException(status_code=400, detail="BIN должен содержать минимум 6 цифр")
    
    # Используем переданный ключ или дефолтный
    key = api_key or BINTABLE_API_KEY
    
    # Если ключ не настроен, используем fallback
    if key == "YOUR_API_KEY_HERE":
        return determine_scheme_by_number(bin)
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BINTABLE_API_URL}/{bin}",
                params={"api_key": key},
                timeout=5.0
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("result") == 200 and data.get("data"):
                    card_data = data["data"].get("card", {})
                    # Получаем схему из API BINTable (это приоритетный источник)
                    scheme = card_data.get("scheme")
                    
                    # Если схема не определена API, используем fallback
                    if not scheme:
                        scheme_result = determine_scheme_by_number(bin)
                        scheme = scheme_result.get("scheme")
                    
                    return {
                        "success": True,
                        "bank": data["data"].get("bank", {}).get("name"),
                        "scheme": scheme,  # Используем схему из API или fallback
                        "country": data["data"].get("country", {}).get("name"),
                        "type": card_data.get("type"),
                    }
            
            # Если API вернул ошибку (например, 401, 429, 404), используем fallback
            return determine_scheme_by_number(bin)
            
    except httpx.TimeoutException:
        # Таймаут - используем fallback
        return determine_scheme_by_number(bin)
    except Exception as e:
        # Любая другая ошибка - используем fallback
        print(f"BIN lookup error: {e}")
        return determine_scheme_by_number(bin)


def determine_scheme_by_number(bin: str) -> dict:
    """
    Определяет схему карты по первым цифрам BIN согласно правилам:
    - Visa: начинается с 4
    - MasterCard: 51-55 или 2221-2720
    - Maestro: 50, 56-58, 67
    - UnionPay: 62
    - Mir: 2200-2204
    """
    if not bin or len(bin) < 2:
        return {
            "success": True,
            "bank": None,
            "scheme": "Unknown",
            "country": None,
            "type": None,
        }
    
    first_digit = bin[0]
    first_two = bin[:2] if len(bin) >= 2 else ""
    first_four = bin[:4] if len(bin) >= 4 else ""
    
    # Visa: начинается с 4
    if first_digit == "4":
        return {
            "success": True,
            "bank": None,
            "scheme": "Visa",
            "country": None,
            "type": None,
        }
    
    # MasterCard: 51-55 (классические)
    if first_two and "51" <= first_two <= "55":
        return {
            "success": True,
            "bank": None,
            "scheme": "Mastercard",
            "country": None,
            "type": None,
        }
    
    # MasterCard: новая серия 2221-2720
    if first_four:
        try:
            first_four_num = int(first_four)
            if 2221 <= first_four_num <= 2720:
                return {
                    "success": True,
                    "bank": None,
                    "scheme": "Mastercard",
                    "country": None,
                    "type": None,
                }
        except ValueError:
            pass
    
    # Maestro: 50, 56-58, 67
    if first_two in ("50", "67") or (first_two and "56" <= first_two <= "58"):
        return {
            "success": True,
            "bank": None,
            "scheme": "Maestro",
            "country": None,
            "type": None,
        }
    
    # UnionPay: начинается с 62
    if first_two == "62":
        return {
            "success": True,
            "bank": None,
            "scheme": "UnionPay",
            "country": None,
            "type": None,
        }
    
    # Mir: начинается с 2200-2204
    if first_four:
        try:
            mir_range = int(first_four)
            if 2200 <= mir_range <= 2204:
                return {
                    "success": True,
                    "bank": None,
                    "scheme": "Мир",
                    "country": None,
                    "type": None,
                }
        except ValueError:
            pass
    
    return {
        "success": True,
        "bank": None,
        "scheme": "Unknown",
        "country": None,
        "type": None,
    }

