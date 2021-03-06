import theano.tensor as t

def cropExtremes(x) :
    # protect the loss function against producing NaNs/Inf
    return t.clip(x, 1e-7, 1.0 - 1e-7)

def crossEntropyLoss (p, q, axis=None, crop=True):
    '''For these purposes this is equivalent to Negative Log Likelihood
       this is the average of all cross-entropies in our guess

       NOTE: This method supports two modes. The target values can be specified
             as either a fmatrix or an ivector. The fmatrix is represented as
             (batchSize, denseTarget). The ivector is a single dimension where 
             the vector length is the batchSize and the number in the ith 
             position is the index into the one hot vector where the one 
             resides. This ivector representation is a more compact way of 
             representing one-hot encodings.

       p    : the target value
       q    : the current estimate
       axis : the axis in which to sum across -- used for multi-dimensional
       crop : crop the extremes to protect against segmentation faults
    '''
    if crop : 
        q = cropExtremes(q)
    if p.ndim > 1 :
        return t.mean(t.sum(t.nnet.binary_crossentropy(q, p), axis=axis))
    else :
        return t.mean(t.nnet.crossentropy_categorical_1hot(q, p))

def meanSquaredLoss (p, q, axis=None) :
    '''for these purposes this is equivalent to Negative Log Likelihood
       p    : the target value
       q    : the current estimate
       axis : the axis in which to sum across -- used for multi-dimensional
    '''
    return t.mean(t.sum((q - p) ** 2, axis=axis))

def calcLoss(p, q, activation) :
    '''Specify a loss function using the last layer's activation.'''
    if q.ndim > 2 :
        axis = q.ndim - 2
    else :  
        axis = 1
    return crossEntropyLoss(p, q, axis) if activation == t.nnet.sigmoid else \
           meanSquaredLoss(p, q, axis)

def calcSparsityConstraint(output, outShape, crop=True) :
    '''Calculate the Kullback-Leibler sparsity based on the number of neurons.

       This constraint favors sparse encodings, thereby enforcing individual
       neurons are accountable for understanding a more robust representation.
    '''
    if crop : 
        output = cropExtremes(output)
    if len(outShape) > 2 :
        import numpy as np
        avgActivation = t.max(output, axis=1)
        sparseCon = 1. / np.prod(outShape[1:])
    else :
        avgActivation = t.mean(output, axis=1)
        sparseCon = 1. / outShape[1]

    return t.mean(sparseCon * t.log(sparseCon / avgActivation) +
                  (1. - sparseCon) * \
                  t.log((1. - sparseCon) / (1. - avgActivation)))

def leastAbsoluteDeviation(a, batchSize=None, scaleFactor=1.) :
    '''L1-norm provides 'Least Absolute Deviation' --
       built for sparse outputs and is resistent to outliers

       a           : input matrix
       batchSize   : number of inputs in the batchs
       scaleFactor : scale factor for the regularization
    '''
    if not isinstance(a, list) :
        a = [a]
    absSum = sum([t.sum(t.abs_(arr)) for arr in a])

    if batchSize is not None :
        return t.mean(absSum // batchSize) * scaleFactor
    else :
        return absSum * scaleFactor

def leastSquares(a, batchSize=None, scaleFactor=1.) :
    '''L2-norm provides 'Least Squares' --
       built for dense outputs and is computationally stable at small errors

       a           : input matrix
       batchSize   : number of inputs in the batchs
       scaleFactor : scale factor for the regularization

       NOTE: a decent scale factor may be the 1. / numNeurons
    '''
    if not isinstance(a, list) :
        a = [a]
    sqSum = sum([t.sum(arr ** 2) for arr in a])

    if batchSize is not None :
        return t.mean(sqSum // batchSize) * scaleFactor
    else :
        return sqSum * scaleFactor

def computeJacobian(a, wrt, batchSize, inputSize, numNeurons) :
    '''Compute a jacobian for the matrix 'out' with respect to 'wrt'.

       This is the first order partials of the output with respect to the 
       weights. This produces a matrix the same size as the input that
       produced the output vector.

       a          : The output matrix for the layer (batchSize, numNeurons)
       wrt        : Matrix used to generate 'mat'. This is usually the weight
                    matrix. (inputSize, numNeurons)
       batchSize  : Number of inputs in the batch
       inputSize  : Size of each input
       numNeurons : Number of neurons in the weight matrix
       return     : (batchSize, inputSize)
    '''
    aReshape = (batchSize, 1, numNeurons)
    wrtReshape = (1, inputSize, numNeurons)
    return t.reshape(a * (1 - a), aReshape) * t.reshape(wrt, wrtReshape)

def compileUpdates(layers, loss) :
    '''Calculate the weight updates required during training.

       layers : Layers of the network to compute updates for
       loss   : Total network loss to apply (ie Error Gradient Summation)
    '''
    import theano
    import numpy as np
    import theano.tensor as t

    updates = []
    for layer in reversed(layers) :

        # pull the rate variables
        layerLearningRate = layer.getLearningRate()
        layerMomentumRate = layer.getMomentumRate()

        # build the gradients
        layerWeights = layer.getWeights()
        gradients = t.grad(loss, layerWeights)#, disconnected_inputs='warn')

        # add the weight update
        for w, g in zip(layerWeights, gradients) :

            if layerMomentumRate > 0. :
                # setup a second buffer for storing momentum
                previousWeightUpdate = theano.shared(
                    np.zeros(w.get_value().shape, theano.config.floatX),
                    borrow=True)

                # add two updates --
                # perform weight update and save the previous update
                updates.append((w, w + previousWeightUpdate))
                updates.append((previousWeightUpdate,
                                previousWeightUpdate * layerMomentumRate -
                                layerLearningRate * g))
            else :
                updates.append((w, w - layerLearningRate * g))

    return updates
