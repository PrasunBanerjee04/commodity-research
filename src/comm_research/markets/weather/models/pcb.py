import numpy as np

from comm_research.common.models.regression import ols
from sklearn.isotonic import IsotonicRegression

class PCB:

    def __init__(self): 
        pass 

    def debiasPP():
        pass 

    def persistencePP():
        pass 

    def predict():
        pass 

    def _isotonic_reg(v: np.array) -> np.array:
        iso = IsotonicRegression(
            y_min=0.0, 
            y_max=1.0, 
            increasing=True
        )
        cdf_proj = iso.fit_transform(np.arange(len(v)), v)
        return cdf_proj