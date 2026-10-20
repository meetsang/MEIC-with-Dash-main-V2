from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time

opts = Options()
opts.add_argument('--headless=new')
opts.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
driver = webdriver.Chrome(options=opts)
try:
    driver.get('http://127.0.0.1:5002/')
    time.sleep(2)
    # Open History tab
    driver.find_element(By.LINK_TEXT, 'History').click()
    time.sleep(2)

    # Click DRMA + H-M pill
    pill = driver.find_element(By.CSS_SELECTOR, "button.hist-strat-pill[data-strat='DRMA_HM_SPX']")
    print('Clicking DRMA + H-M pill...')
    pill.click()
    time.sleep(2)

    for entry in driver.get_log('browser'):
        print('Browser log after pill click:', entry['level'], entry['message'])

    rows = driver.find_elements(By.CSS_SELECTOR, "#history-tbody tr")
    print(f'Rows count after DRMA click: {len(rows)}')
    if rows:
        print('First row text:', rows[0].text)

    # Now pick a date
    date_input = driver.find_element(By.ID, "hist-nav-date")
    print('Current hist-nav-date:', date_input.get_attribute('value'))
    print('Current filter-date:', driver.find_element(By.ID, "filter-date").get_attribute('value'))
    
    # Try selecting from session dropdown
    sel = driver.find_element(By.ID, "hist-session-select")
    options = [o.get_attribute('value') for o in sel.find_elements(By.TAG_NAME, 'option')]
    print('hist-session-select options:', options[:5])
    
    if len(options) > 1:
        target = options[1]
        print('Selecting session:', target)
        sel.send_keys(target)
        # trigger change
        driver.execute_script("setHistoryDateFilter(arguments[0])", target)
        time.sleep(2)
        rows2 = driver.find_elements(By.CSS_SELECTOR, "#history-tbody tr")
        print(f'Rows count after session select: {len(rows2)}')
        if rows2:
            print('First row text:', rows2[0].text)

finally:
    driver.quit()
