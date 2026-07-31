import requests
import time
import json
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import List, Optional, Dict, Any
import logging

class KrakenAPI:
    """
    Robust Kraken REST API client with retry logic, rate limiting, and error handling.
    """
    
    BASE_URL = "https://api.kraken.com/0"
    
    def __init__(self, base_url: str = BASE_URL, timeout: int = 8, max_retries: int = 2):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        
        # Configure session with retry strategy
        self.session = requests.Session()
        
        # Exponential backoff retry strategy
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        logging.info("KrakenAPI initialized with timeout=%s, max_retries=%s", timeout, max_retries)
    
    def _build_url(self, endpoint: str) -> str:
        """Build full API URL"""
        return f"{self.base_url}/{endpoint.lstrip('/')}"
    
    def _handle_response(self, response: requests.Response) -> Any:
        """Handle API response with error checking"""
        try:
            response.raise_for_status()
            data = response.json()
            
            # Check for Kraken API errors
            if "error" in data and data["error"]:
                error_msg = "; ".join(data["error"])
                logging.error("Kraken API error: %s", error_msg)
                return None
            
            return data.get("result")
            
        except requests.exceptions.Timeout:
            logging.error("Request timed out after %s seconds", self.timeout)
            return None
        except requests.exceptions.RequestException as e:
            logging.error("Request failed: %s", str(e))
            return None
        except json.JSONDecodeError:
            logging.error("Failed to parse JSON response")
            return None
    
    def get_ohlc(self, pair: str, interval: int = 1, since: Optional[int] = None) -> Optional[pd.DataFrame]:
        """
        Fetch OHLC data for a single trading pair.
        
        Args:
            pair: Trading pair (e.g., "BTC/USD")
            interval: Time interval in minutes (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)
            since: Return trade data since given id (optional)
        
        Returns:
            DataFrame with OHLC data or None on error
        """
        endpoint = f"public/OHLC"
        params = {
            "pair": pair,
            "interval": interval
        }
        
        if since:
            params["since"] = since
        
        try:
            response = self.session.get(
                self._build_url(endpoint),
                params=params,
                timeout=self.timeout
            )
            
            result = self._handle_response(response)
            if result is None:
                return None
            
            # Kraken returns data as {pair_key: [[timestamp, open, high, low, close, vwap, volume, count], ...], "last": ...}
            # Key may be "XXBTZUSD" instead of "BTC/USD" — find the data array
            pair_data = result.get(pair)
            if not pair_data:
                # Try alternate key formats (Kraken uses internal names like XXBTZUSD)
                for k, v in result.items():
                    if k != 'last' and isinstance(v, list):
                        pair_data = v
                        break
            if not pair_data:
                logging.warning("No data returned for pair %s", pair)
                return None
            
            # Convert to DataFrame
            df = pd.DataFrame(pair_data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 
                'vwap', 'volume', 'count'
            ])
            
            # Convert timestamp to datetime
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            df.set_index('timestamp', inplace=True)
            
            # Convert numeric columns
            for col in ['open', 'high', 'low', 'close', 'vwap', 'volume', 'count']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # Drop rows with NaN in critical columns (vwap can be 0 for low-liquidity periods)
            df = df.dropna(subset=['open', 'high', 'low', 'close'])
            
            logging.info("Fetched OHLC data for %s: %d rows", pair, len(df))
            return df
            
        except Exception as e:
            logging.error("Failed to fetch OHLC for %s: %s", pair, str(e))
            return None
    
    def get_multiple_ohlc(self, pairs: List[str], interval: int = 1) -> Dict[str, Optional[pd.DataFrame]]:
        """
        Fetch OHLC data for multiple trading pairs in parallel.
        
        Args:
            pairs: List of trading pairs
            interval: Time interval in minutes
        
        Returns:
            Dictionary mapping pair to DataFrame or None
        """
        results = {}
        for pair in pairs:
            # Add small delay to avoid rate limiting
            time.sleep(0.1)
            df = self.get_ohlc(pair, interval)
            results[pair] = df
        
        return results
    
    def get_tradable_pairs(self) -> Optional[Dict[str, Any]]:
        """Get list of tradable pairs"""
        endpoint = "public/AssetPairs"
        try:
            response = self.session.get(
                self._build_url(endpoint),
                timeout=self.timeout
            )
            return self._handle_response(response)
        except Exception as e:
            logging.error("Failed to fetch tradable pairs: %s", str(e))
            return None

# Research notes:
# - Kraken allows max 50 pairs per request for OHLC
# - Recommended polling interval: 1-5 minutes for OHLC
# - Rate limits: ~15 requests/second for public endpoints
# - Use exponential backoff for reliability
# - Always validate data for NaN and out-of-range values

if __name__ == "__main__":
    # Test the API
    api = KrakenAPI()
    
    # Get tradable pairs
    pairs = api.get_tradable_pairs()
    if pairs:
        pair_list = list(pairs.keys())[:5]  # Test with first 5 pairs
        print(f"Testing with pairs: {pair_list}")
        
        # Fetch OHLC data
        results = api.get_multiple_ohlc(pair_list, interval=1)
        
        for pair, df in results.items():
            if df is not None:
                print(f"{pair}: {len(df)} rows, last timestamp: {df.index[-1]}")
            else:
                print(f"{pair}: failed to fetch")