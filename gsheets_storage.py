import gspread
from google.oauth2.service_account import Credentials
import logging
import os
import json

class GoogleSheetsStorage:
    def __init__(self):
        self.sheet = None
        self.setup_sheets()
    
    def setup_sheets(self):
        """Настройка подключения к Google Sheets"""
        try:
            # Получаем credentials из переменной окружения
            creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
            if not creds_json:
                logging.error("❌ GOOGLE_CREDENTIALS_JSON not found")
                return None
            
            # ID таблицы из переменной окружения
            sheet_id = os.getenv('GOOGLE_SHEET_ID')
            if not sheet_id:
                logging.error("❌ GOOGLE_SHEET_ID not found")
                return None
            
            # Аутентификация
            scopes = ['https://www.googleapis.com/auth/spreadsheets']
            creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
            client = gspread.authorize(creds)
            
            # Открываем таблицу
            self.sheet = client.open_by_key(sheet_id)
            logging.info("✅ Google Sheets connected successfully")
            
        except Exception as e:
            logging.error(f"❌ Google Sheets setup failed: {e}")
            self.sheet = None
    
    def ensure_worksheet(self, name):
        """Создает или возвращает лист"""
        try:
            if not self.sheet:
                return None
            
            try:
                worksheet = self.sheet.worksheet(name)
            except gspread.WorksheetNotFound:
                worksheet = self.sheet.add_worksheet(title=name, rows=1000, cols=2)
                worksheet.update('A1:B1', [['key', 'value']])
            
            return worksheet
        except Exception as e:
            logging.error(f"❌ Worksheet error: {e}")
            return None
    
    def save_data(self, data_name, data):
        """Сохраняет данные в указанный лист"""
        try:
            worksheet = self.ensure_worksheet(data_name)
            if not worksheet:
                return False
            
            # Очищаем старые данные (кроме заголовка)
            worksheet.clear()
            worksheet.update('A1:B1', [['key', 'value']])
            
            # Преобразуем данные в список
            rows = []
            for key, value in data.items():
                rows.append([key, json.dumps(value, ensure_ascii=False)])
            
            if rows:
                worksheet.update(f'A2:B{len(rows)+1}', rows)
            
            logging.info(f"✅ Data saved to {data_name}: {len(data)} items")
            return True
            
        except Exception as e:
            logging.error(f"❌ Save data error: {e}")
            return False
    
    def load_data(self, data_name):
        """Загружает данные из указанного листа"""
        try:
            worksheet = self.ensure_worksheet(data_name)
            if not worksheet:
                return {}
            
            data = worksheet.get_all_records()
            result = {}
            
            for row in data:
                if 'key' in row and 'value' in row:
                    try:
                        result[row['key']] = json.loads(row['value'])
                    except:
                        result[row['key']] = row['value']
            
            logging.info(f"✅ Data loaded from {data_name}: {len(result)} items")
            return result
            
        except Exception as e:
            logging.error(f"❌ Load data error: {e}")
            return {}

# Глобальный экземпляр
gsheets_storage = GoogleSheetsStorage()