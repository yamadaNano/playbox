import theano.tensor as t
import theano
from nn.net import ClassifierNetwork
from ae.encoder import AutoEncoder
import numpy as np

class SAENetwork (ClassifierNetwork) :
    '''The SAENetwork object allows autoencoders to be stacked such that the 
       output of one autoencoder becomes the input to another. This network
       creates the necessary connections to stack the autoencoders.

       This object provides basic encoding through the classify, and
       classifyAndSoftmax functionality provided by the base class.

       filepath : Path to an already trained network on disk 
                  'None' creates randomized weighting
       prof     : Profiler to use
    '''
    def __init__ (self, filepath=None, prof=None) :
        ClassifierNetwork.__init__(self, filepath, prof)

    def addLayer(self, encoder) :
        '''Add an autoencoder to the network.'''
        if not isinstance(encoder, AutoEncoder) :
            raise TypeError('addLayer is expecting a AutoEncoder object.')
        ClassifierNetwork.addLayer(self, encoder)

    # TODO: these should both be removed!
    def getLayer(self, layerIndex) :
        return self._layers[layerIndex]
    def writeWeights(self, layerIndex, epoch) :
        self._layers[layerIndex].writeWeights(epoch)

class ClassifierSAENetwork (SAENetwork) :
    '''The ClassifierSAENetwork adds the ability to classify a provided input
       against a target encoding feature matrix. The user must provide an
       example input(s), which the network then uses to determine similities
       between the feature data and the provided input.

       target   : numpy.ndarray of inputs used for target data. This is the way
                  to provide the unsupervised learning algorithm with a means
                  to perform classification.
       filepath : Path to an already trained network on disk 
                  'None' creates randomized weighting
       prof     : Profiler to use
    '''
    def __init__ (self, targetData, filepath=None, prof=None) :
        SAENetwork.__init__(self, filepath, prof)
        self._targetData = targetData

    def __getstate__(self) :
        '''Save network pickle'''
        dict = SAENetwork.__getstate__(self)
        # remove the training and test datasets before pickling. This both
        # saves disk space, and makes trained networks allow transfer learning
        dict['_targetData'] = None
        if '_targetEncodings' in dict : del dict['_targetEncodings']
        # remove the functions -- they will be rebuilt JIT
        if '_closeness' in dict : del dict['_closeness']
        return dict

    def __setstate__(self, dict) :
        '''Load network pickle'''
        # remove any current functions from the object so we force the
        # theano functions to be rebuilt with the new buffers
        if hasattr(self, '_closeness') : delattr(self, '_closeness')
        # preserve the user specified softmaxTemp
        if hasattr(self, '_targetData') : 
            tmp = self._targetData
        SAENetwork.__setstate__(self, dict)
        if hasattr(self, '_targetData') : 
            self._targetData = tmp

    def finalizeNetwork(self, networkInput) :
        '''Setup the network based on the current network configuration.
           This creates several network-wide functions so they will be
           pre-compiled and optimized when we need them.
        '''
        from theano import dot, function
        from dataset.shared import toShared
        import numpy as np

        if len(self._layers) == 0 :
            raise IndexError('Network must have at least one layer' +
                             'to call getNetworkInput().')

        self._startProfile('Finalizing Network', 'info')

        # disable the profiler temporarily so we don't get a second entry
        tmp = self._profiler
        self._profiler = None
        SAENetwork.finalizeNetwork(self, networkInput)
        self._profiler = tmp

        # ensure targetData is at least one batchSize, otherwise enlarge
        batchSize = self.getNetworkInputSize()[0]
        numTargets = self._targetData.shape[0]
        if numTargets < batchSize :
            # add rows of zeros to fill out the rest of the batch
            self._targetData = np.resize(np.append(
                np.zeros([batchSize-numTargets] +
                         list(self._targetData.shape[1:]), 
                         np.float32), self._targetData),
                [batchSize] + list(self._targetData.shape[1:]))

        # produce the encoded feature matrix --
        # this matrix will be used for all closeness calculations
        #
        # classify the inputs one batch at a time
        enc = []
        for ii in range(int(numTargets / batchSize)) :
            enc.extend(self.classifyAndSoftmax(
                self._targetData[ii*batchSize:(ii+1)*batchSize])[1])

        # run one last batch and collect the remainder --
        # this is also used if there is less than one batch worth of targets
        remainder = numTargets % batchSize
        if remainder > 0 :
            enc.extend(self.classifyAndSoftmax(
                self._targetData[-batchSize:])[1][-remainder:])
        # NOTE: this is the transpose to orient for matrix multiplication
        self._targetEncodings = toShared(enc, borrow=True).T

        # TODO: Check if this should be the raw logit from the output layer or
        #       the softmax return of the output layer.
        # TODO: This needs to be updated to handle matrix vs matrix cosine
        #       similarities between all pairs of vectors
        # setup the closeness execution graph based on target information
        targets = t.fmatrix('targets')
        outClass = t.nnet.softmax(self.getNetworkOutput()[0])
        cosineSimilarity = dot(outClass, targets) / \
            (t.sqrt(t.sum(outClass**2)) * (t.sqrt(t.sum(targets**2))))
        self._closeness = function([self.getNetworkInput()[0]],
                                   cosineSimilarity.T,
                                   givens={targets: self._targetEncodings})

    def closeness(self, inputs) :
        '''This is a form of classification for SAE networks. The network has
           been provided a target input, which we now use to determine the
           similarity of this input against that target set. 

           NOTE: Response of 1.0 indicates equality. The lower number indicate
                 less overlap between features.
        '''
        from dataset.shared import toShared
        self._startProfile('Determining Closeness of Inputs', 'debug')
        if not hasattr(self, '_closeness') :
            inp = toShared(inputs, borrow=True) \
                  if 'SharedVariable' not in str(type(inputs)) else inputs
            self.finalizeNetwork(inp[:])
        if not hasattr(self, '_targetEncodings') :
            raise ValueError('User must finalize the feature matrix before ' +
                             'attempting to finalize the network.')

        # test out similar this input is compared with the targets
        cosineMatrix = self._closeness(inputs)
        self._endProfile()
        return cosineMatrix

    def closenessAndEncoding (self, inputs) :
        '''This is a form of classification for SAE networks. The network has
           been provided a target input, which we now use to determine the
           similarity of this input against that target set. 

           This method is additionally setup to return the raw encoding for the
           inputs provided.

           return : (classification index, softmax vector)
        '''
        self._startProfile('Determining Closeness of Inputs', 'debug')

        # TODO: this needs to be updated if the encodings should not be the
        #       result of a softmax on the logits.
        cosineMatrix, encodings = (self.closeness(inputs), 
                                   self.classifyAndSoftmax(inputs)[1])
        self._endProfile()
        return cosineMatrix, encodings


class TrainerSAENetwork (SAENetwork) :
    '''The TrainerSAENetwork object expands on the classification and allows
       allows training of the stacked autoencoder both in a greedy-layerwise
       and network-wide manner. 

       NOTE: The resulting trained AEs can be used to initialize a 
             nn.TrainerNetwork.

       train    : theano.shared dataset used for network training in format --
                  (numBatches, batchSize, numChannels, rows, cols)
       filepath : Path to an already trained network on disk 
                  'None' creates randomized weighting
       prof     : Profiler to use
    '''
    def __init__ (self, train, filepath=None, prof=None) :
        SAENetwork.__init__ (self, filepath, prof)
        self._indexVar = t.lscalar('index')
        self._trainData = train[0] if isinstance(train, list) else train
        self._numTrainBatches = self._trainData.shape.eval()[0]
        self._trainGreedy = []

    def __buildEncoder(self) :
        '''Build the greedy-layerwise function --
           All layers start with the input original input, however are updated
           in a layerwise manner.
           NOTE: this uses theano.shared variables for optimized GPU execution
        '''
        layerInput = (self._trainData[0], self._trainData[0])
        for encoder in self._layers :
            # forward pass through layers
            self._startProfile('Finalizing Encoder [' + encoder.layerID + ']', 
                               'debug')
            encoder.finalize(layerInput)
            out, up = encoder.getUpdates()
            self._trainGreedy.append(
                theano.function([self._indexVar], out, updates=up,
                                givens={self.getNetworkInput()[1] : 
                                        self._trainData[self._indexVar]}))
            layerInput = encoder.output
            self._endProfile()

    def __buildDecoder(self) :
        '''Build the decoding section and the network-wide training method.'''
        from nn.costUtils import calcLoss, calcSparsityConstraint

        # setup the decoders -- 
        # this is the second half of the network and is equivalent to the
        # encoder network reversed.
        layerInput = self._layers[-1].output[1]
        sparseConstr = calcSparsityConstraint(layerInput, 
                                              self.getNetworkOutputSize())

        jacobianCost = 0
        for decoder in reversed(self._layers) :
            # backward pass through layers
            self._startProfile('Finalizing Decoder [' + decoder.layerID + ']', 
                               'debug')
            jacobianCost += decoder.getUpdates()[0][1]
            layerInput = decoder.buildDecoder(layerInput)
            self._endProfile()
        decodedInput = layerInput

        # TODO: here we assume the first layer uses sigmoid activation
        self._startProfile('Setting up Network-wide Decoder', 'debug')
        cost = calcLoss(self._trainData[0], decodedInput, t.nnet.relu)
        costs = [cost, jacobianCost, sparseConstr]

        # build the network-wide training update.
        updates = []
        for decoder in reversed(self._layers) :

            # build the gradients
            layerWeights = decoder.getWeights()
            gradients = t.grad(t.sum(costs), layerWeights, 
                               disconnected_inputs='warn')

            # add the weight update
            for w, g in zip(layerWeights, gradients) :
                updates.append((w, w - decoder.getLearningRate() * g))

        #from theano.compile.nanguardmode import NanGuardMode
        self._trainNetwork = theano.function(
            [self._indexVar], costs, updates=updates, 
            givens={self.getNetworkInput()[1] : 
                    self._trainData[self._indexVar]})
            #mode=NanGuardMode(nan_is_error=True, inf_is_error=True, big_is_error=True))
        self._endProfile()

    def __getstate__(self) :
        '''Save network pickle'''
        dict = ClassifierSAENetwork.__getstate__(self)
        # remove the functions -- they will be rebuilt JIT
        if '_indexVar' in dict : del dict['_indexVar']
        if '_trainData' in dict : del dict['_trainData']
        if '_numTrainBatches' in dict : del dict['_numTrainBatches']
        if '_trainGreedy' in dict : del dict['_trainGreedy']
        if '_trainNetwork' in dict : del dict['_trainNetwork']
        return dict

    def __setstate__(self, dict) :
        '''Load network pickle'''
        self._indexVar = t.lscalar('index')
        # remove any current functions from the object so we force the
        # theano functions to be rebuilt with the new buffers
        if hasattr(self, '_trainGreedy') : delattr(self, '_trainGreedy')
        if hasattr(self, '_trainNetwork') : delattr(self, '_trainNetwork')
        self._trainGreedy = []
        ClassifierSAENetwork.__setstate__(self, dict)

    def finalizeNetwork(self, networkInputs) :
        '''Setup the network based on the current network configuration.
           This is used to create several network-wide functions so they will
           be pre-compiled and optimized when we need them. The only function
           across all network types is classify()
        '''
        if len(self._layers) == 0 :
            raise IndexError('Network must have at least one layer' +
                             'to call finalizeNetwork().')

        self._startProfile('Finalizing Network', 'info')

        self.__buildEncoder()

        # disable the profiler temporarily so we don't get a second entry
        tmp = self._profiler
        self._profiler = None
        ClassifierSAENetwork.finalizeNetwork(self, networkInputs)
        self._profiler = tmp

        self.__buildDecoder()
        self._endProfile()

    def train(self, layerIndex, index) :
        '''Train the network against the pre-loaded inputs. This accepts
           a batch index into the pre-compiled input set.
           layerIndex : specify which layer to train
           index      : specify a pre-compiled mini-batch index
           inputs     : DEBUGGING Specify a numpy tensor mini-batch
        '''
        self._startProfile('Training Batch [' + str(index) +
                           '/' + str(self._numTrainBatches) + ']', 'debug')
        if not hasattr(self, '_trainGreedy') or \
           not hasattr(self, '_trainNetwork') :
            self.finalizeNetwork(self._trainData[0])
        if not isinstance(index, int) :
            raise Exception('Variable index must be an integer value')
        if index >= self._numTrainBatches :
            raise Exception('Variable index out of range for numBatches')

        # train the input --
        # the user decides whether this will be a greedy or network training
        # by passing in a layer index. If the index does not have an associated
        # layer, it automatically chooses network-wide training.
        if layerIndex < 0 or layerIndex >= self.getNumLayers() :
            ret = self._trainNetwork(index)
        else :
            ret = self._trainGreedy[layerIndex](index)

        self._endProfile()
        return ret

    def trainEpoch(self, layerIndex, globalEpoch, numEpochs=1) :
        '''Train the network against the pre-loaded inputs for a user-specified
           number of epochs.

           layerIndex  : index of the layer to train
           globalEpoch : total number of epochs the network has previously 
                         trained
           numEpochs   : number of epochs to train this round before stopping
        '''
        globCost = []
        for localEpoch in range(numEpochs) :
            layerEpochStr = 'Layer[' + str(layerIndex) + '] Epoch[' + \
                            str(globalEpoch + localEpoch) + ']'
            self._startProfile('Running ' + layerEpochStr, 'info')
            locCost = []
            for ii in range(self._numTrainBatches) :
                locCost.append(self.train(layerIndex, ii))
            '''
            reconstructedInput = self._layers[layerIndex].reconstruction(
                                    self._trainData.get_value(borrow=True)[0])
            from dataset.debugger import saveTiledImage
            saveTiledImage(
                image=reconstructedInput,
                path=self._layers[layerIndex].layerID + '_reconstruction_' + 
                     str(globalEpoch+localEpoch) + '.png',
                imageShape=tuple(self._layers[layerIndex].getInputSize()[-2:]),
                spacing=1,
                interleave=True)
            '''
            locCost = np.mean(locCost, axis=0)
            self._startProfile(layerEpochStr + ' Cost: ' + \
                               str(locCost[0]) + ' - Jacob: ' + \
                               str(locCost[1]) + ' - Sparsity: ' + \
                               str(locCost[2]), 'info')
            globCost.append(locCost)

            self._endProfile()
            self._endProfile()

            #self.writeWeights(layerIndex, globalEpoch + localEpoch)
        return globalEpoch + numEpochs, globCost
