import os
import sys

# Dynamically find the project root
current_dir = os.path.abspath(os.path.dirname(__file__))
while current_dir and current_dir != os.path.dirname(current_dir):
    if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)
        break
    current_dir = os.path.dirname(current_dir)
import time,json
from tradingbook import tb_config as config
from tradingbook import tb_update
from app import utilities as utility
from openpyxl import load_workbook

def main():
    orders_file = config.ORDERS_FILE
    trd_bk_file_path = config.TRADINGBOOK

    # Backup the files before Update
    tb_update.backup_tradingbook()
    time.sleep(2)

    # Create log File
    log = utility.get_logger("tradingbook","tradingbook.log")


    #CREATE FINAL DATA FRAME FDF
    fdf = tb_update.create_df(orders_file)    

    # Displaying the DataFrame
    print(fdf)    
    log.info(fdf)    

    # Load the Excel file for updating 
    book = load_workbook(trd_bk_file_path)
    tb_update.update_sheets(book,fdf)
    # Save the updated Excel file
    book.save(trd_bk_file_path)
    log.info("Trading Book Updated Successfully")
    print("Trading Book Updated Successfully")

    #Update Order File to Empty data
    with open(orders_file, 'w') as file:
        json.dump({}, file, indent=2)
    log.info("Orders File Updated Successfully")
    print("Orders File Updated Successfully")


if __name__ == "__main__":
    main()