import os

# Dynamically find the project root
current_dir = os.path.abspath(os.path.dirname(__file__))
project_root = current_dir
while current_dir and current_dir != os.path.dirname(current_dir):
    if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
        project_root = current_dir
        break
    current_dir = os.path.dirname(current_dir)

TRADINGBOOK = os.path.join(project_root, 'meic0dte', 'tradingbook', 'tradingbook.xlsx')
TRDBKBCKP = os.path.join(project_root, 'meic0dte', 'tradingbook', 'tradingbook_backup.xlsx')

ORDERS_FILE = os.path.join(project_root, 'meic0dte', 'app', 'order_params.json')
ORDER_FILE_BCKP = os.path.join(project_root, 'meic0dte', 'tradingbook')
