# cloud_tensoRF
## run training
```
python train_adapt.py --config configs/adapt_ship/ship_adapt_0.4_0.2.txt
```
## Scripts
```
train_hier.py 
    hierachical cloud tensorf, without rotation (rotgrad is false in default)
train_adapt.py 
    non hierachical, but enable rotation with pca / clustering
train_dbasis.py 
    zexiang's share vm cloud tensorf. each local tensorf use a shared matrix but its own vectors
```
