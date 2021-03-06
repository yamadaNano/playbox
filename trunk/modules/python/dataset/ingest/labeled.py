import os
import numpy as np
import theano.tensor as t

def checkAvailableMemory(dataMemoryConsumption, shared, log) :
    '''There are three possible cases of memory consumption:
       1. GPU has enough memory, thus load the dataset directly to the device.
          This will achieve the best performance as transfers to the GPU over
          PCIe will starve its processing.
       2. GPU is insufficient on memory, but it can fit in CPU memory. We
          load the entire dataset in CPU memory and transfer over PCIe
          just-in-time for processing.
       3. CPU and GPU memory is insufficient. In this case we rely on disk IO
          to support training on this large dataset. Performance will be
          extremely dampened while running in this mode, however training will
          be possible.

       NOTE: Cases 2 & 3 are hidden from the user by using HDF5. No additional
             care will need to be take between these two processing types.
    '''
    import psutil

    convertToGB = 1. / (1024. * 1024. * 1024.)
    memoryConsumGB = str(dataMemoryConsumption * convertToGB)
    if log is not None :
        log.info('Dataset will consume [' + memoryConsumGB + '] GBs')

    # check if the machine is capable of loading dataset into CPU memory
    oneGigMem = 2 ** 30
    availableCPUMem = psutil.virtual_memory()[1] - oneGigMem
    if availableCPUMem > dataMemoryConsumption :
        if log is not None :
            log.debug('Dataset will fit in CPU memory. [' + 
                      str(availableCPUMem * convertToGB) + '] GBs available.')
    else :
        if log is not None :
            log.warn('Dataset is too large for CPU memory. Dataset will ' +
                     'be memory mapped and backed by disk IO.')

    # if the user wants to use the GPU check if the dataset can be loaded 
    # entirely into shared memory
    if 'gpu' in t.config.device and shared :
        import theano.sandbox.cuda.basic_ops as sbcuda

        # the user has requested this goes into GPU memory if possible --
        # NOTE: this check is by no means guaranteed. There must be enough
        #       contigous memory on the device for a successful allocation.
        #       Below we handle the case where this check passes, but
        #       the allocation ultimately fails.
        availableGPUMem = sbcuda.cuda_ndarray.cuda_ndarray.mem_info()[0]
        if availableGPUMem > dataMemoryConsumption :
            if log is not None :
                log.debug('Dataset will fit in GPU memory. [' + 
                          str(availableGPUMem * convertToGB) + 
                          '] GBs available.')
        else :
            if log is not None :
                log.warn('Dataset is too large for GPU memory. Dataset will ' + 
                         'be transferred over PCIe just-in-time. ')
            shared = False

    return shared

def readDataset(trainDataH5, train, trainShape, batchSize, threads, log) :
    import theano
    from six.moves import queue
    import threading
    from dataset.reader import padImageData, readImage

    # add jobs to the queue --
    # NOTE : h5py.Dataset doesn't implement __setslice__, so we must implement
    #        the copy via __setitem__. This differs from my normal index
    #        formatting, but it gets the job done.
    workQueueData = queue.Queue()
    for ii in range(trainShape[0]) :
        workQueueData.put((trainDataH5, np.s_[ii, :], trainShape[1:],
                           train[ii*batchSize:(ii+1)*batchSize], log))

    # stream the imagery into the buffers --
    # we are threading this for efficiency
    def readImagery() :
        while True :
            dataH5, sliceIndex, batchSize, imageFiles, log = \
                workQueueData.get()

            # allocate a load the batch locally so our write are coherent
            tmp = np.ndarray((batchSize), theano.config.floatX)
            for ii, imageFile in enumerate(imageFiles) :
                tmp[ii][:] = padImageData(readImage(imageFile[0], log),
                                          batchSize[-3:])[:]
            dataH5[sliceIndex] = tmp[:]

            workQueueData.task_done()

    # create the workers
    for ii in range(threads) :
        thread = threading.Thread(target=readImagery)
        thread.daemon = True
        thread.start()

    # join the threads and complete
    workQueueData.join()

def readAndDivideData(path, holdoutPercentage, minTest=5, log=None) :
    '''This walks the directory structure and divides the data according to the
       user specified holdout over two "train" and "test" sets.
    '''
    from dataset.shuffle import naiveShuffle

    # read the directory structure --
    # each subdirectory becomes a label and the imagery within are examples.
    # Splitting the data per label ensures each category is represented in the
    # holdout set.
    train, test, labels = [], [], []
    for root, dirs, files in os.walk(path) :
        if root == path :
            continue
        if len(files) == 0 :
            if log is not None :
                log.debug('No files found in [' + root + ']')
            continue

        # add the new label for the current directory
        label = os.path.relpath(root, path).replace(os.path.sep, '.')
        labels.append(label)
        indx = len(labels) - 1
        if log is not None :
            log.debug('Adding directory [' + root + '] as [' + label + ']')

        # a small percentage of the data is held out to verify our training
        # isn't getting overfitted. We will randomize the input later.
        numTest = max(minTest, int(holdoutPercentage * len(files)))
        if log is not None :
            log.debug('Holding out [' + str(numTest) + '] of [' + \
                      str(len(files)) + ']')

        # this ensures a minimum number of examples are used for testing
        holdoutPercentage = (float(numTest) / float(len(files)))

        # use the most common type of file in the dir and exclude
        # other types (avoid generated jpegs, other junk)
        suffix = mostCommonSuffix(files, samplesize=50)

        # prepare the data
        items = np.asarray(
            [(os.path.join(root, file), indx) for file in files
                if file.endswith(suffix)],
            dtype=np.object)
        naiveShuffle(items)

        # randomly distribute the data using Random Assignment based on
        # Bernoulli trials
        # TODO: There may be a more compact way to represent this in python
        randomAssign = np.random.binomial(1, holdoutPercentage, len(items))
        train.extend(
            [items[ii] for ii in range(len(items)) if randomAssign[ii] == 0])
        test.extend(
            [items[ii] for ii in range(len(items)) if randomAssign[ii] == 1])

    # randomize the data across categories -- otherwise its not stochastic
    if log is not None :
        log.info('Shuffling the data for randomization')
    naiveShuffle(train)
    naiveShuffle(test)

    return train, test, labels

def hdf5Dataset(filepath, holdoutPercentage=.05, minTest=5,
                batchSize=1, log=None) :
    '''Create a hdf5 file out of a directory structure. The directory structure
       is assumed to be a series of directories, each contains imagery assigned
       the label of the directory name.

       filepath          : Top-level directory containing the label directories
       holdoutPercentage : Percentage of the data to holdout for testing
       minTest           : Hard minimum on holdout if percentage is low
       batchSize         : Size of a mini-batch
       log               : Logger to use
    '''
    import theano
    import threading
    import multiprocessing
    from six.moves import queue
    from dataset.reader import getImageDims, mostCommon
    from dataset.hdf5 import createHDF5Labeled

    rootpath = os.path.abspath(filepath)
    outputFile = os.path.join(rootpath, os.path.basename(rootpath) + 
                              '_labeled' + 
                              '_holdout_' + str(holdoutPercentage) +
                              '_batch_' + str(batchSize) +'.hdf5')
    if os.path.exists(outputFile) :
        if log is not None :
            log.info('HDF5 exists for this dataset [' + outputFile +
                     ']. Using this instead.')
        return outputFile

    # walk the directory structure
    if log is not None :
        log.info('Reading the directory structure')

    # we support two possible operations --
    # If there are train/ and test/ directories in the target directories, we
    # use the user provided breakdown of the data and ignore the holdout
    # parameter. Otherwise, we walk the unified directory structure and divide
    # the data according to the holdoutPercentage.
    trainDir = os.path.join(rootpath, 'train')
    testDir = os.path.join(rootpath, 'test')
    if os.path.isdir(trainDir) and os.path.isdir(testDir) :

        # run each directory to grab the imagery
        trainSet = readAndDivideData(trainDir, 0., 0, log)
        testSet = readAndDivideData(testDir, 1., 0, log)

        # verify the labels overlap
        for label in testSet[-1] :
            if label not in trainSet[-1] :
                raise ValueError('Train and Test sets have non-overlapping ' +
                                 'labels. Please check the input directory.')

        # setup for further processing
        train, test, labels = trainSet[0], testSet[1], trainSet[-1]

    else :
        # split the data according to the user-provided holdout
        train, test, labels = readAndDivideData(rootpath, holdoutPercentage,
                                                minTest, log)

    if len(train) == 0 :
        raise ValueError('No training examples found [' + filepath + ']')

    # create a hdf5 memmap --
    # Here we create the handles to the data buffers. This operates on the
    # assumption the dataset may not fit entirely in memory. The handles allow
    # data to overflow and ultimately be stored entirely on disk. 
    #
    # Sample the directory for a probable chip size
    size = mostCommon((t[0] for t in train),
                      lambda f: os.path.getsize(f),
                      sampleSize=50)
    sizedFile = next((t[0] for t in train if os.path.getsize(t[0]) == size))
    imageShape = list(getImageDims(sizedFile, log))

    trainShape = [len(train) // batchSize, batchSize] + imageShape
    # TODO: performs a floor, so if there is less than one batch no
    #       data will be returned. Add a check for this.
    trainShape = [len(train) // batchSize, batchSize] + imageShape
    testShape = [len(test) // batchSize, batchSize] + imageShape

    [handleH5, trainDataH5, trainIndicesH5, 
     testDataH5, testIndicesH5, labelsH5] = \
        createHDF5Labeled (outputFile, 
                           trainShape, theano.config.floatX, np.int32,
                           testShape, theano.config.floatX, np.int32,
                           len(labels), log)

    if log is not None :
        log.info('Writing data to HDF5')

    # populate the indice buffers
    workQueueIndices = queue.Queue()
    workQueueIndices.put((trainIndicesH5, trainIndicesH5, train))
    workQueueIndices.put((testIndicesH5, testIndicesH5, test))

    # stream the indices into the buffers --
    # we are threading this for efficiency
    def copyIndices() :
        while True :
            dataH5, indicesH5, data = workQueueIndices.get()
            dataH5[:] = np.resize(np.asarray(data).flatten()[1::2],
                                  indicesH5.shape).astype(np.int32)[:]
            workQueueIndices.task_done()
    for ii in range(2) :
        thread = threading.Thread(target=copyIndices)
        thread.daemon = True
        thread.start()
    workQueueIndices.join()

    # read the image data
    threads = multiprocessing.cpu_count()
    readDataset(trainDataH5, train, trainShape, batchSize, threads, log)
    readDataset(testDataH5, test, testShape, batchSize, threads, log)

    # stream in the label in string form
    labelsH5[:] = labels[:]

    if log is not None :
        log.info('Flushing to disk')

    # write it to disk    
    handleH5.flush()
    handleH5.close()

    # return the output filename
    return outputFile

def ingestImagery(filepath, shared=True, log=None, **kwargs) :
    '''Load the labeled dataset into memory. This is formatted such that the
       directory structure becomes the labels, and all imagery within the 
       directory will be assigned this label. All images in any directory is
       required to have the same dimensions.

       filepath : This can be a cPickle, a path to the directory structure.
       shared   : Load data into shared variables for training --
                  NOTE: this is only a user suggestion. However the size of the
                        data will ultimately determine how its loaded.
       log      : Logger for tracking the progress
       kwargs   : Any parameters needed to override defaults in hdf5Dataset
       return   :
           Format -- 
           (trainData, trainLabel), (testData, testLabel), labels

           The 'trainLabel' and 'testLabel' are integer values corresponding
           to the index into the 'labels' string vector. this provides a
           better means to identify errors during back propagation, but
           still allows label finding during classification.

           TODO: Consider returning these as objects for more intuitive
                 indexing. For now numpy indexing is sufficient.
    '''
    import os
    from dataset.shared import splitToShared
    from dataset.hdf5 import readHDF5

    if not os.path.exists(filepath) :
        raise ValueError('The path specified does not exist.')

    # read the directory structure and pickle it up
    if os.path.isdir(filepath) :
        filepath = hdf5Dataset(filepath, log=log, **kwargs)

    # Load the dataset to memory
    train, test, labels = readHDF5(filepath, log)

    # calculate the memory needed by this dataset
    dt = [4., 4., 4., 4.] if t.config.floatX == 'float32' else [8., 4., 8., 4.]
    dataMemoryConsumption = \
        np.prod(np.asarray(train[0].shape, dtype=np.float32)) * dt[0] + \
        np.prod(np.asarray(train[1].shape, dtype=np.float32)) * dt[1] + \
        np.prod(np.asarray(test[0].shape,  dtype=np.float32)) * dt[2] + \
        np.prod(np.asarray(test[1].shape,  dtype=np.float32)) * dt[3]

    # check physical memory constraints
    shared = checkAvailableMemory(dataMemoryConsumption, shared, log)

    # load each into shared variables -- 
    # this avoids having to copy the data to the GPU between each call
    if shared is True :
        if log is not None :
            log.debug('Transfer the memory into shared variables')
        try :
            tr = splitToShared(train)
            te = splitToShared(test)
            return tr, te, labels
        except :
            pass
    return train, test, labels
