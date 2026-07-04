import pandas as pd, shutil, os, json
from tradingbook import tb_config as config
from app import config as app_config
from app import utilities as _util
from openpyxl import load_workbook

from datetime import datetime as dt

#Take a backup of the Tradingbook before updating
def backup_tradingbook():
    src_file = config.TRADINGBOOK
    dst_folder = config.TRDBKBCKP

    order_src_file = config.ORDERS_FILE
    order_dst_folder = config.ORDER_FILE_BCKP


    try:
        shutil.copy2(src_file, dst_folder)
        print("Tradingbook File copied successfully.")
    except FileExistsError:
        # If the file already exists in the destination folder, replace it
        os.remove(os.path.join(dst_folder, os.path.basename(src_file)))
        shutil.copy2(src_file, dst_folder)
        print("Tradingbook File replaced successfully.")

    try:
        shutil.copy2(order_src_file, f"{order_dst_folder}/{_util.central_date().strftime('%Y%m%d')}_meic_orders.json")
        print("Orders File copied successfully.")
    except FileExistsError:
        # If the file already exists in the destination folder, replace it
        os.remove(f"{order_dst_folder}/{_util.central_date().strftime('%Y%m%d')}_meic_orders.json")
        shutil.copy2(order_src_file, f"{order_dst_folder}/{_util.central_date().strftime('%Y%m%d')}_meic_orders.json")
        print("Orders File replaced successfully.")

def update_sheets(book,fdf):
    daily_pnl = 0.0
    am_daily_pnl = 0.0

    
    # Update Lot Sheets
    for lot in fdf['Lot']:
        date_opened =  _util.central_date().strftime("%Y-%m-%d")
        row_id = fdf[fdf['Lot'] == lot].index[0]    
        # Get the sheet by lot 
        sheet = book["Trades"]
        #Get Current Values from the sheet serial no and cumulative pnl
        sno = sheet['A3'].value
        sno+=1
        sheet.insert_rows(3)

        sheet['A3'] = sno
        sheet['B3'] = date_opened
        sheet['C3'] = lot
        sheet['D3'] = fdf['P_SP'].iloc[row_id]
        sheet['E3'] = fdf['P_LP'].iloc[row_id]
        sheet['F3'] = fdf['P_qty'].iloc[row_id]
        sheet['G3'] = fdf['P_Opn_SP_Price'].iloc[row_id]
        sheet['H3'] = fdf['P_Opn_LP_Price'].iloc[row_id]
        sheet['I3'] = fdf['P_credit'].iloc[row_id]
        sheet['J3'] = fdf['P_Cls_SP_Price'].iloc[row_id]
        sheet['K3'] = fdf['P_Cls_LP_Price'].iloc[row_id]
        sheet['L3'] = fdf['P_debit'].iloc[row_id]
        sheet['M3'] = fdf['P_fees'].iloc[row_id]
        sheet['N3'] = fdf['P_pnl'].iloc[row_id]
        sheet['O3'] = fdf['C_SP'].iloc[row_id]
        sheet['P3'] = fdf['C_LP'].iloc[row_id]
        sheet['Q3'] = fdf['C_qty'].iloc[row_id]
        sheet['R3'] = fdf['C_Opn_SP_Price'].iloc[row_id]
        sheet['S3'] = fdf['C_Opn_LP_Price'].iloc[row_id]
        sheet['T3'] = fdf['C_credit'].iloc[row_id]
        sheet['U3'] = fdf['C_Cls_SP_Price'].iloc[row_id]
        sheet['V3'] = fdf['C_Cls_LP_Price'].iloc[row_id]
        sheet['W3'] = fdf['C_debit'].iloc[row_id]
        sheet['X3'] = fdf['C_fees'].iloc[row_id]
        sheet['Y3'] = fdf['C_pnl'].iloc[row_id]
        sheet['Z3'] = fdf['Total_Fee'].iloc[row_id]
        pnl = fdf['Total_PnL'].iloc[row_id]
        sheet['AA3'] = pnl

        daily_pnl+=pnl

    # Update Total Sheet
    sheet = book['MEIC_Total']
    tsno = sheet['A2'].value
    tsno+=1
    cumpnl = sheet['D2'].value
    cumpnl+=daily_pnl
    sheet.insert_rows(2)
    sheet['A2'] = tsno
    sheet['B2'] = date_opened
    sheet['C2'] = daily_pnl
    sheet['D2'] = cumpnl


def create_df(orders_file):
    with open(orders_file, 'r') as file:
        data = json.load(file)
    # Create an empty list to store individual records
    records = []
    
    def calc_fees(price_list,qty):
        fees = 0
        for price in price_list:
            if price == 0:
                fee = 0
            elif price > 0:
                fee = 0.47+0.65
            elif price >= 1:
                fee = 0.56+0.65
            fees+=fee
        return round(fees*qty,2)

    date_opened =  _util.central_date().strftime("%Y-%m-%d")

    # Iterate over each Lot and extract information
    for lot, orders in data.items():
        # Reset all values for next iteration
        C_SP, C_LP, P_SP, P_LP = "-", "-", "-", "-"
        C_qty, C_Opn_SP_Price, C_Opn_LP_Price, C_credit, C_Cls_SP_Price, C_Cls_LP_Price, C_debit, C_fees, C_pnl = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        P_qty, P_Opn_SP_Price, P_Opn_LP_Price, P_credit, P_Cls_SP_Price, P_Cls_LP_Price, P_debit, P_fees, P_pnl = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0  

        for opt_type,order in orders.items():
            print(f"Lot: {lot}, OPT Type: {opt_type}")
            if opt_type == "C":                
                C_SP = order['short_symbol'][-7:-3]
                C_LP = order['long_symbol'][-7:-3]
                C_qty = order['filled_quantity']
                C_Opn_SP_Price = order['short_open_price']
                C_Opn_LP_Price = order['long_open_price']
                C_credit = C_Opn_SP_Price-C_Opn_LP_Price
                C_Cls_SP_Price = order['short_close_price']
                C_Cls_LP_Price = order['long_close_price']
                C_debit = C_Cls_SP_Price-C_Cls_LP_Price
                C_fees = calc_fees([C_Opn_SP_Price,C_Opn_LP_Price,C_Cls_SP_Price,C_Cls_LP_Price],C_qty)
                C_pnl = round(((C_credit-C_debit)*C_qty*100)-(C_fees),2)                
            elif opt_type == "P":
                P_SP = order['short_symbol'][-7:-3]
                P_LP = order['long_symbol'][-7:-3]
                P_qty = order['filled_quantity']
                P_Opn_SP_Price = order['short_open_price']
                P_Opn_LP_Price = order['long_open_price']
                P_credit = P_Opn_SP_Price-P_Opn_LP_Price
                P_Cls_SP_Price = order['short_close_price']
                P_Cls_LP_Price = order['long_close_price']
                P_debit = P_Cls_SP_Price-P_Cls_LP_Price
                P_fees = calc_fees([P_Opn_SP_Price,P_Opn_LP_Price,P_Cls_SP_Price,P_Cls_LP_Price],P_qty)
                P_pnl = round(((P_credit-P_debit)*P_qty*100)-(P_fees),2)
                
                
        total_fees = C_fees+P_fees
        # calculate final pnl with multiplier
        total_pnl = C_pnl+P_pnl
        record = {            
            'Lot': lot, 
            'Date': date_opened,   
            'P_SP' : P_SP,
            'P_LP' : P_LP,
            'P_qty' : P_qty,
            'P_Opn_SP_Price' : P_Opn_SP_Price,
            'P_Opn_LP_Price' : P_Opn_LP_Price,
            'P_credit' : P_credit,
            'P_Cls_SP_Price' : P_Cls_SP_Price,
            'P_Cls_LP_Price' : P_Cls_LP_Price,
            'P_debit' : P_debit,
            'P_fees' : P_fees,
            'P_pnl' : P_pnl,
            'C_SP' : C_SP,
            'C_LP' : C_LP,
            'C_qty' : C_qty,
            'C_Opn_SP_Price' : C_Opn_SP_Price,
            'C_Opn_LP_Price' : C_Opn_LP_Price,
            'C_credit' : C_credit,
            'C_Cls_SP_Price' : C_Cls_SP_Price,
            'C_Cls_LP_Price' : C_Cls_LP_Price,
            'C_debit' : C_debit,
            'C_fees' : C_fees,
            'C_pnl' : C_pnl,            
            'Total_Fee' : total_fees,
            'Total_PnL' : total_pnl
        }
        records.append(record)
       

    # Creating DataFrame from the list of records
    df = pd.DataFrame(records)
    return df