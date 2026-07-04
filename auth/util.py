import logging

#=============================================================================================================================#

class TerminateRequest(Exception):
    def __init__(self,message):
        super().__init__(message)
        print(f"SCHWAB AUTH ERROR: {message}")


#=============================================================================================================================#

#Defining Individual Thread logger fundtion
def get_logger(name, log_file, level=logging.DEBUG):
    handler = logging.FileHandler(f"./logs/{log_file}",mode='w')     
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)   
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    return logger

#=============================================================================================================================#