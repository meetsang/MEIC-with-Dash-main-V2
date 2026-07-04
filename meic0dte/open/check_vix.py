from common.vixspx import open_vixspx

def compare_vix(bearer,lot,log):

    last_vix,open_vix_price,vix_prcnt_chng= open_vixspx.get_overnight_prcntchng(bearer,"$VIX",lot,log)
        
    if open_vix_price >= last_vix:
        log.info(f"Current VIX Price: {open_vix_price}, Last VIX Price: {last_vix} - VIX is UP")
        return True
    else:
        log.info(f"Current VIX Price: {open_vix_price}, Last VIX Price: {last_vix} - VIX is DOWN")
        return False