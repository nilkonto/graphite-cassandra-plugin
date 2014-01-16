from bisect import bisect_left
from itertools import izip
import json
import os
from os import path
import sys

from pycassa import ConsistencyLevel, ConnectionPool, ColumnFamily,\
   NotFoundException
from pycassa.cassandra.ttypes import ConsistencyLevel
from pycassa.system_manager import SystemManager, time
from pycassa.types import UTF8Type

DEFAULT_TIMESTEP = 60
DEFAULT_SLICE_CACHING_BEHAVIOR = 'none'

# dev code to log using the same logger as graphite web or carbon
import logging
log_info = logging.getLogger("info").info

class NodeCache(object):
  """A cache for :class:`DataNode` objects
  
  TODO: this may have retention policy and size limits.
  """
  def __init__(self):
    self._cache = {}

  def add(self, key, node):
    """Adds or replaces the `node` in the cache.
    """
    self._cache[key] = node

  def get(self, key):
    """Gets the node from the cache with `key` or None if no key exists.
    """
    
    try:
      return self._cache[key]
    except (KeyError):
      return None

class DataTree(object):
  """Represents a tree of Ceres metrics contained within a single path on disk
  This is the primary Ceres API.

  :param root: The directory root of the Ceres tree

  See :func:`setDefaultSliceCachingBehavior` to adjust caching behavior
  """
  
  def __init__(self, root, keyspace, server_list, 
    read_consistency_level=ConsistencyLevel.ONE, 
    write_consistency_level=ConsistencyLevel.ONE):
    
    self.cassandra_connection = ConnectionPool(keyspace, server_list)
    self.root = root
    self._cache = NodeCache()
    self.read_consistency_level = read_consistency_level
    self.write_consistency_level = write_consistency_level
    
    #TODO: make CL configurable.
    self._metadata_cf = ColumnFamily(self.cassandra_connection, 
      'metadata', read_consistency_level=self.read_consistency_level, 
      write_consistency_level=self.write_consistency_level)
    self._data_tree_cf = ColumnFamily(self.cassandra_connection, 
      'data_tree_nodes', read_consistency_level=self.read_consistency_level, 
      write_consistency_level=self.write_consistency_level)
      
  def __repr__(self):
    return "<DataTree[0x%x]: %s>" % (id(self), self.root)
  __str__ = __repr__

  def hasNode(self, nodePath):
    """Returns whether the Ceres tree contains the given metric"""
    
    # TODO: check the cache.
    log_info("DataTree.hasNode(): metadata.get(%s)" % (nodePath,))
    try:
      # faster to read a named column
      self._metadata_cf.get(nodePath, columns=["metadta"])
      return True
    except (NotFoundException):
       return False

  def getNode(self, nodePath):
    """Returns a Ceres node given a metric name

      Raises :exc:`NodeNotFound` if the node is not found.
      
      :param nodePath: A metric name

      :returns: :class:`DataNode`
    """
    
    existing = self._cache.get(nodePath)
    if existing is not None:
      return existing
    
    log_info("DataTree.getNode(): metadata.get(%s)" % (nodePath,))
    try:
      data = self._metadata_cf.get(nodePath, columns=["metadata"])
    except (NotFoundException) as e:
      raise NodeNotFound("Node %s not found" % (nodePath))
    
    meta_data = json.loads(data["metadata"])
    node = DataNode(self, meta_data, nodePath, nodePath)
    self._cache.add(node.nodePath, node)
    return node


  def createNode(self, nodePath, **properties):
    """Creates a new metric given a new metric name and optional per-node 
      metadata
      
      :keyword nodePath: The new metric name.
      :keyword \*\*properties: Arbitrary key-value properties to store as 
      metric metadata.

      :returns: :class:`DataNode`
    """
    return DataNode.create(self, properties, nodePath)

  def store(self, nodePath, datapoints):
    """Store a list of datapoints associated with a metric
    
      :keyword nodePath: The metric name to write to
      :keyword datapoints: A list of datapoint tuples: (timestamp, value)
    """
    
    self.getNode(nodePath).write(datapoints)
    return
    
  def getFilesystemPath(self, nodePath):
    """Get the on-disk path of a Ceres node given a metric name
    """
    return path.join(self.root, nodePath.replace('.', os.sep))

  def getNodePath(self, fsPath):
    """Get the metric name of a Ceres node given the on-disk path"""
    return fsPath

  def getSliceInfo(self, query):
    """ return all slice info for a given query
      This needs to get a single level of the tree
      Think of it in terms of a glob:
        - * at the top of the tree should return all of the root-level nodes
        - carbon.* should return anything that begins with carbon, but *only*
          replacing .* with the actual value:
          ex. carbon.metrics.
              carbon.tests.
    """
    if query == '*':
      query = 'root'
    else:
      query = query.replace('.*', '')

    log_info("DataTree.getSliceInfo(): data_tree_nodes.get(%s)" % (query,))
    try:
      return self._data_tree_cf.get(query)
    except (NotFoundException):
      # empty dict to say there is are no sub nodes.
      return {}


class DataNode(object):
  __slots__ = ('tree', 'nodePath', 'fsPath',
               'metadataFile', 'timeStep',
               'sliceCache', 'sliceCachingBehavior', 'cassandra_connection', 
               '_meta_data')

  def __init__(self, tree, meta_data, nodePath, fsPath):
    self.tree = tree
    self.nodePath = nodePath
    self.fsPath = nodePath
    #self.metadataFile = path.join(fsPath, '.ceres-node')
    self.metadataFile = nodePath
    self.timeStep = None
    # sliceCache is sometimes a list of DataSlice objects and sometimes
    # a single DataSlice object. 
    # TODO: Consider make it a list at all times.
    self.sliceCache = None
    self.sliceCachingBehavior = DEFAULT_SLICE_CACHING_BEHAVIOR
    self.cassandra_connection = tree.cassandra_connection
    
    self._meta_data = meta_data
    self.timeStep = self._meta_data.get("timeStep")
    
  def __repr__(self):
    return "<DataNode[0x%x]: %s>" % (id(self), self.nodePath)
  __str__ = __repr__

  @classmethod
  def create(cls, tree, meta_data, nodePath):
    """Construct a new DataNode with the `node_path` and `meta_data` 
    in the `tree` and store it.
    """
    meta_data.setdefault('timeStep', DEFAULT_TIMESTEP)
    node = cls(tree, meta_data, nodePath, nodePath)
    node.writeMetadata(properties)
    return node


  @property
  def slice_info(self):
    return [
      (si.startTime, si.endTime, si.timeStep) 
      for si in self.slices
    ]

  def readMetadata(self):
    return self._meta_data

  def writeMetadata(self, metadata):
    log_info("DataNode.writeMetadata(): metadata.insert(%s)" % (
      self.metadataFile,))

    if not 'startTime' in metadata:
      metadata['startTime'] = int(time.time())
    self._metadata_cf.insert(self.metadataFile, 
      {'metadata': json.dumps(metadata)})

  @property
  def slices(self):
    
    # What happens when the sliceCache is null
    if self.sliceCache:
      if self.sliceCachingBehavior == 'all':
        for data_slice in self.sliceCache:
          yield data_slice

      elif self.sliceCachingBehavior == 'latest':
        # TODO: This yielding the entire list, think it should return 
        # the last item in the list
        yield self.sliceCache
        infos = self.readSlices()
        # TODO: Why does this skip the first item ?
        for info in infos[1:]:
          # TODO: this is passing startTime and timeStep
          # remove the * call and pass proper args
          yield DataSlice(self, *info)

    else:
      if self.sliceCachingBehavior == 'all':
        # TODO: this is passing startTime and timeStep
        # remove the * call and pass proper args
        self.sliceCache = [
          DataSlice(self, *info) 
          for info in self.readSlices()
        ]
        for data_slice in self.sliceCache:
          yield data_slice

      elif self.sliceCachingBehavior == 'latest':
        infos = self.readSlices()
        if infos:
          # TODO: this is passing startTime and timeStep
          # remove the * call and pass proper args
          self.sliceCache = DataSlice(self, *infos[0])
          yield self.sliceCache

        for info in infos[1:]:
          # TODO: this is passing startTime and timeStep
          # remove the * call and pass proper args
          yield DataSlice(self, *info)

      elif self.sliceCachingBehavior == 'none':
        for info in self.readSlices():
          # TODO: this is passing startTime and timeStep
          # remove the * call and pass proper args
          yield DataSlice(self, *info)

      else:
        raise ValueError("invalid caching behavior configured '%s'" % (
          self.sliceCachingBehavior,))

  def readSlices(self):
    return [
        (int(self._meta_data["startTime"]), 
        int(self._meta_data["timeStep"]))
    ]
    # slice_info = []
    # rowName = "{0}".format(self.nodePath)
    # log_info("DataNode.readSlices(): metadata.get(%s)" % (rowName,))
    # try:
    #   client = ColumnFamily(self.cassandra_connection, 'metadata')
    #   values = client.get(rowName)
    #   metadata = json.loads(values['metadata'])
    #   slice_info.append((int(metadata['startTime']), int(metadata['timeStep'])))
    #   #for _, value in values:
    #   #  startTime, timeStep = value.popitem()
    #   #  slice_info.append((int(startTime), int(timeStep)))
    # 
    #   #slice_info.sort(reverse=True)
    # except Exception:
    #     pass
    # 
    # return slice_info

  def setSliceCachingBehavior(self, behavior):
    behavior = behavior.lower()
    if behavior not in ('none', 'all', 'latest'):
      raise ValueError("invalid caching behavior '%s'" % behavior)

    self.sliceCachingBehavior = behavior
    self.sliceCache = None
    return

  def clearSliceCache(self):
    self.sliceCache = None
    return

  def hasDataForInterval(self, fromTime, untilTime):
    slices = list(self.slices)
    if not slices:
      return False
      
    # Why is this getting the last item in the list and first ? 
    # Are these guaranteed to be in descending order ? 
    # slices returns DataSlice objects.
    earliestData = slices[-1].startTime
    latestData = slices[0].endTime

    return ((fromTime is None) or (fromTime < latestData)) and \
           ((untilTime is None) or (untilTime > earliestData))

  def read(self, fromTime, untilTime):
    
    if self.timeStep is None:
      self.readMetadata()

    # Normalize the timestamps to fit proper intervals
    fromTime = int(fromTime - (fromTime % self.timeStep) + self.timeStep)
    untilTime = int(untilTime - (untilTime % self.timeStep) + self.timeStep)

    sliceBoundary = None  # to know when to split up queries across slices
    resultValues = []
    earliestData = None

    # TODO: slice is a global function, rename this var
    # We are iterating over DataSlice objects here
    for slice in self.slices:
      # if the requested interval starts after the start of this slice
      if fromTime >= slice.startTime:
        try:
          series = slice.read(fromTime, untilTime)
        except NoData:
          # Should this break processing, means we only look at one dataslice?
          # it's a continue below
          break

        earliestData = series.startTime

        rightMissing = (untilTime - series.endTime) / self.timeStep
        rightNulls = [None for i in range(rightMissing - len(resultValues))]
        resultValues = series.values + rightNulls + resultValues
        break

      # or if slice contains data for part of the requested interval
      elif untilTime >= slice.startTime:
        # Split the request up if it straddles a slice boundary
        if (sliceBoundary is not None) and untilTime > sliceBoundary:
          requestUntilTime = sliceBoundary
        else:
          requestUntilTime = untilTime

        try:
          series = slice.read(slice.startTime, requestUntilTime)
        except NoData:
          continue

        earliestData = series.startTime

        rightMissing = (requestUntilTime - series.endTime) / self.timeStep
        rightNulls = [None for i in range(rightMissing)]
        resultValues = series.values + rightNulls + resultValues

      # this is the right-side boundary on the next iteration
      sliceBoundary = slice.startTime

    # The end of the requested interval predates all slices
    if earliestData is None:
      missing = int(untilTime - fromTime) / self.timeStep
      resultValues = [None for i in range(missing)]

    # Left pad nulls if the start of the requested interval predates all slices
    else:
      leftMissing = (earliestData - fromTime) / self.timeStep
      leftNulls = [None for i in range(leftMissing)]
      resultValues = leftNulls + resultValues

    return TimeSeriesData(fromTime, untilTime, self.timeStep, resultValues)

  def write(self, datapoints):
    if self.timeStep is None:
      self.readMetadata()

    if not datapoints:
      return

    sequences = self.compact(datapoints)
    needsEarlierSlice = []  # keep track of sequences that precede all existing slices

    while sequences:
      sequence = sequences.pop()
      timestamps = [t for t,v in sequence]
      beginningTime = timestamps[0]
      endingTime = timestamps[-1]
      sliceBoundary = None  # used to prevent writing sequences across slice boundaries
      slicesExist = False

      for slice in self.slices:
        if slice.timeStep != self.timeStep:
          continue

        slicesExist = True

        # truncate sequence so it doesn't cross the slice boundaries
        if beginningTime >= slice.startTime:
          if sliceBoundary is None:
            sequenceWithinSlice = sequence
          else:
            # index of highest timestamp that doesn't exceed sliceBoundary
            boundaryIndex = bisect_left(timestamps, sliceBoundary)
            sequenceWithinSlice = sequence[:boundaryIndex]

          try:
            slice.write(sequenceWithinSlice)
          except SliceGapTooLarge:
            newSlice = DataSlice.create(self, beginningTime, slice.timeStep)
            newSlice.write(sequenceWithinSlice)
            self.sliceCache = None
          except SliceDeleted:
            self.sliceCache = None
            self.write(datapoints)  # recurse to retry
            return

          break

        # sequence straddles the current slice, write the right side
        elif endingTime >= slice.startTime:
          # index of lowest timestamp that doesn't preceed slice.startTime
          boundaryIndex = bisect_left(timestamps, slice.startTime)
          sequenceWithinSlice = sequence[boundaryIndex:]
          leftover = sequence[:boundaryIndex]
          sequences.append(leftover)
          slice.write(sequenceWithinSlice)

        else:
          needsEarlierSlice.append(sequence)

        sliceBoundary = slice.startTime

      if not slicesExist:
        sequences.append(sequence)
        needsEarlierSlice = sequences
        break

    for sequence in needsEarlierSlice:
      # TODO: what is sequence[0][0] returning
      slice = DataSlice.create(self, int(sequence[0][0]), self.timeStep)
      slice.write(sequence)
      self.sliceCache = None

  def compact(self, datapoints):
    datapoints = sorted((int(timestamp), float(value))
                         for timestamp, value in datapoints
                         if value is not None)
    sequences = []
    sequence = []
    minimumTimestamp = 0  # used to avoid duplicate intervals

    for timestamp, value in datapoints:
      timestamp -= timestamp % self.timeStep  # round it down to a proper interval

      if not sequence:
        sequence.append((timestamp, value))

      else:
        if not timestamp > minimumTimestamp:  # drop duplicate intervals
          continue

        if timestamp == sequence[-1][0] + self.timeStep:  # append contiguous datapoints
          sequence.append((timestamp, value))

        else:  # start a new sequence if not contiguous
          sequences.append(sequence)
          sequence = [(timestamp, value)]

      minimumTimestamp = timestamp

    if sequence:
      sequences.append(sequence)

    return sequences


class DataSlice(object):
  __slots__ = ('node', 'cassandra_connection', 'startTime', 'timeStep', 'fsPath', 'retention')

  def __init__(self, node, startTime, timeStep):
    self.node = node
    self.cassandra_connection = node.cassandra_connection
    self.startTime = startTime
    self.timeStep = timeStep
    self.fsPath = "{0}".format(node.fsPath)

    # TODO: pull from cache within DataNode.readMetadata
    metadata = self.node.readMetadata()
    retention = filter(lambda x:x[0] == self.timeStep, metadata['retentions'])[0]
    self.retention = int(retention[0]) * int(retention[1])

  def __repr__(self):
    return "<DataSlice[0x%x]: %s>" % (id(self), self.fsPath)
  __str__ = __repr__

  @property
  def isEmpty(self):
    count = 0
    log_info("DataSlice.isEmpty(): " + "ts{0}".format(self.timeStep) + ".get(%s)" % (rowName,))
    
    try:
      client = ColumnFamily(self.cassandra_connection, ("ts{0}".format(self.timeStep)))
      rowName = "{0}".format(self.node.fsPath)
      count = client.get(rowName, column_count=1)
    except Exception:
      return True
    return count == 0

  @property
  def endTime(self):
    client = ColumnFamily(self.cassandra_connection, ("ts{0}".format(self.timeStep)))
    rowName = "{0}".format(self.node.fsPath)
    log_info("DataSlice.endTime(): "  + "ts{0}".format(self.timeStep) +  ".get(%s, reversed)" % (rowName,))
    try:
      last_value = client.get(rowName, column_reversed=True, column_count=1)
      return int(timestamp.keys()[-1])
    except Exception:
      return time.time()

  @classmethod
  def create(cls, node, startTime, timeStep):
    slice = cls(node, startTime, timeStep)
    return slice

  def read(self, fromTime, untilTime):
    timeOffset = int(fromTime) - self.startTime

    if timeOffset < 0:
      raise InvalidRequest("requested time range ({0}, {1}) preceeds this slice: {2}".format(fromTime, untilTime, self.startTime))

    values = []
    rowName = "{0}".format(self.node.fsPath)
    log_info("DataSlice.read(): "  + "ts{0}".format(self.timeStep) +  ".get(%s)" % (rowName,))
    try:
      client = ColumnFamily(self.cassandra_connection, ("ts{0}".format(self.timeStep)))
      # TODO: Use Cassandra's maximum row limit here. This 1.99 billion is done
      # so that we don't limit the query at all
      values = client.get(rowName, column_start="{0}".format(fromTime), column_finish="{0}".format(untilTime), column_count=1999999999)
    except NotFoundException:
      pass
    except Exception as e:
      raise Exception('DataSlice.read error: %s' % str(e))

    if len(values) <= 0:
      raise NoData()

    endTime = values.keys()[-1]
    #print '[DEBUG slice.read] startTime=%s fromTime=%s untilTime=%s' % (self.startTime, fromTime, untilTime)
    #print '[DEBUG slice.read] timeInfo = (%s, %s, %s)' % (fromTime, endTime, self.timeStep)
    #print '[DEBUG slice.read] values = %s' % str(values)
    values = [float(x) for x in values.values()]
    return TimeSeriesData(fromTime, int(endTime), self.timeStep, values)

  def check_for_metric_table(self, tablename=''):
    cass_server = self.cassandra_connection.server_list[0]
    keyspace = self.cassandra_connection.keyspace

    sys_manager = SystemManager(cass_server)
    log_info("DataSlice.check_for_metric_table(): sys_manager.get_keyspace_column_families(%s)" % (keyspace,))
    cf_defs = sys_manager.get_keyspace_column_families(keyspace)

    if tablename not in cf_defs.keys():
      sys_manager.create_column_family(
          keyspace,
          tablename,
          super=False,
          comparator_type=UTF8Type(),
          key_validation_class=UTF8Type(),
          default_validation_class=UTF8Type()
      )

  def insert_metric(self, metric, client, isMetric=False):
    split = metric.split('.')
    if len(split) == 1:
      log_info("DataSlice.insert_metric() 1: %s.insert(%s)" % (client.name, "root",))
      client.insert('root', { metric : '' })
    else:
      next_metric = '.'.join(split[0:-1])
      metric_type =  'metric' if isMetric else ''
      log_info("DataSlice.insert_metric() 2: %s.insert(%s)" % (client.name, next_metric,))
      client.insert(next_metric, {'.'.join(split) : metric_type })
      self.insert_metric(next_metric, client)

  def write(self, sequence):
    try:
      rowName = "{0}".format(self.node.fsPath)
      tableName = "ts{0}".format(self.timeStep)

      # Make sure that the table exists
      self.check_for_metric_table(tableName)
      # Add the metric
      client = ColumnFamily(self.cassandra_connection, tableName)
      for t, v in sequence:
        log_info("DataSlice.write() 1: %s.insert(%s)" % (tableName, rowName,))
        client.insert(rowName, { str(t) : str(v) }, ttl=self.retention)
    except Exception as e:
      raise Exception("DataSlice.write 1 error: {0}".format(e))

    # update the slide info for the timestamp lookup
    #TODO: Evaluate if anything in this try block is necessary
    try:
      #client = ColumnFamily(self.cassandra_connection, 'metadata')
      #client.insert(self.node.fsPath, { str(self.startTime) : str(self.timeStep)})

      client = ColumnFamily(self.cassandra_connection, 'metadata')
      rowName = "{0}".format(self.node.fsPath)
      # TODO :This will eventually be replaced with something that hits cache
      log_info("DataSlice.write() 2: metadata.get(%s)" % (rowName,))
      metadata = client.get(rowName)
      metadata = json.loads(metadata['metadata'])
      if not 'startTime' in metadata:
        metadata['startTime'] = self.startTime
        log_info("DataSlice.write() 3: metadata.insert(%s)" % (rowName,))
        client.insert(rowName, {'metadata' : json.dumps(metadata)})
    except Exception as e:
      raise Exception("DataSlice.write 2 error: {0}".format(str(e)))

    try:
      client = ColumnFamily(self.cassandra_connection, 'data_tree_nodes')
      # Strip off the metric name
      #split_metric = '.'.join(self.node.fsPath.split('.')[0:-1])
      #self.insert_metric(split_metric, client)
      rowName = "{0}".format(self.node.fsPath)
      log_info("DataSlice.write() 4: data_tree_nodes.insert(%s)" % (rowName,))
      client.insert(rowName, {'metric' : 'true'})
      self.insert_metric(rowName, client, True)
    except Exception as e:
      raise Exception("DataSlice.write 3 error: {0}".format(str(e)))

  def __cmp__(self, other):
    return cmp(self.startTime, other.startTime)



class TimeSeriesData(object):
  __slots__ = ('startTime', 'endTime', 'timeStep', 'values')

  def __init__(self, startTime, endTime, timeStep, values):
    self.startTime = startTime
    self.endTime = endTime
    self.timeStep = timeStep
    self.values = values

  @property
  def timestamps(self):
    return xrange(self.startTime, self.endTime, self.timeStep)

  def __iter__(self):
    return izip(self.timestamps, self.values)

  def __len__(self):
    return len(self.values)

  def merge(self, other):
    for timestamp, value in other:
      if value is None:
        continue

      timestamp -= timestamp % self.timeStep
      if timestamp < self.startTime:
        continue

      index = int((timestamp - self.startTime) / self.timeStep)

      try:
        if self.values[index] is None:
          self.values[index] = value
      except IndexError:
        continue


class CorruptNode(Exception):
  def __init__(self, node, problem):
    Exception.__init__(self, problem)
    self.node = node
    self.problem = problem


class NoData(Exception):
  pass


class NodeNotFound(Exception):
  pass


class NodeDeleted(Exception):
  pass


class InvalidRequest(Exception):
  pass


class SliceGapTooLarge(Exception):
  "For internal use only"


class SliceDeleted(Exception):
  pass



def setDefaultSliceCachingBehavior(behavior):
  # TODO Should this be global?
  global DEFAULT_SLICE_CACHING_BEHAVIOR

  behavior = behavior.lower()
  if behavior not in ('none', 'all', 'latest'):
    raise ValueError("invalid caching behavior '%s'" % behavior)

  DEFAULT_SLICE_CACHING_BEHAVIOR = behavior


def initializeTableLayout(keyspace, server_list=[]):
    try:
      # TODO randomize server selection
      # TODO move functionality out of try statement
      cass_server = server_list[0]
      sys_manager = SystemManager(cass_server)

      # Make sure the the keyspace exists
      if keyspace not in sys_manager.list_keyspaces():
        sys_manager.create_keyspace(keyspace, SIMPLE_STRATEGY, \
                {'replication_factor': '3'})

      cf_defs = sys_manager.get_keyspace_column_families(keyspace)

      # Loop through and make sure that the necessary column families exist
      for tablename in ["data_tree_nodes", "metadata"]:
        if tablename not in cf_defs.keys():
          sys_manager.create_column_family(
              keyspace,
              tablename,
              super=False,
              comparator_type=UTF8Type(),
              key_validation_class=UTF8Type(),
              default_validation_class=UTF8Type()
          )
    except Exception as e:
      raise Exception("Error initalizing table layout: {0}".format(e))


