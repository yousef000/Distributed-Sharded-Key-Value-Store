from flask import Flask, request, Response, abort, jsonify, json
import requests
import re
import sys
import os
import time
import hashlib

DICTIONARY = None

REPLICAS = os.environ.get('VIEW').split(',')  
SHARDS = {}     #Dict of shard # to list of nodes
SOCKET = os.environ.get('SOCKET_ADDRESS')
try:
    SHARD_COUNT = int(os.environ.get('SHARD_COUNT'))
except:
    SHARD_COUNT = None
app = Flask(__name__)

current_shard = None
vectorclock = {}
versionlist = []

###################### Shard Operations ######################
@app.route('/key-value-store-shard/node-shard-id', methods=['GET'])
def getNodeShardId():
    global current_shard
    data={"message":"Shard ID of the node retrieved successfully", 
        "shard-id":str(current_shard)}
    return app.response_class(response=json.dumps(data),status=200,mimetype='application/json')

@app.route('/key-value-store-shard/shard-ids', methods=['GET'])
def shardIds():
    global SHARDS
    data = {"message":"Shard IDs retrieved successfully",
            "shard-ids":",".join([str(x) for x in SHARDS.keys()])}
    return app.response_class(response=json.dumps(data),status=200,mimetype='application/json')    

@app.route('/key-value-store-shard/shard-id-members/<id>', methods=['GET'])
def shardMembers(id):
    global SHARDS
    data = {"message":"Members of shard ID retrieved successfully",
            "shard-id-members":",".join(getNodesInShard(int(id)))}
    return app.response_class(response=json.dumps(data), status=200,mimetype='application/json')    

@app.route('/key-value-store-shard/shard-id-key-count/<shardid>',methods = ['GET'])
def keyCount(shardid):
    global SHARDS
    total = 0 
    shardid = int(shardid)
    node = SHARDS[shardid][0]
    print(SHARDS[shardid],file = sys.stderr)

    URL = 'http://' + node + '/request-dict/'
    response = requests.get(url=URL)
    responseInJson = response.json()
    total = total + len(responseInJson['kvs'])
    print(responseInJson['kvs'],file = sys.stderr)
    data = {"message":"Key count of shard ID retrieved successfully",
        "shard-id-key-count":total}
    return app.response_class(response=json.dumps(data), status=200,mimetype='application/json')    

@app.route('/key-value-store-shard/add-member/<shard>', methods = ['PUT'])
def addNodeToShards(shard):
    global SHARDS
    address = request.get_json()['socket-address']
    print('----- Adding ',address,'to node',shard,'-----', file=sys.stderr)
    SHARDS[int(shard)].append(address)
    broadcastShardOverwrite()
    data = {"assigned-id":shard}
    requests.put(url='http://'+address+'/assign-shard/'+shard)
    print('New Shard View:',SHARDS,file=sys.stderr)
    print('----- Node added -----',file=sys.stderr)
    return app.response_class(response=json.dumps(data), status=200,mimetype='application/json')    

@app.route('/key-value-store-shard/reshard', methods=['PUT'])
def reshard():
    global current_shard
    global SHARDS
    global SHARD_COUNT
    global SOCKET
    global REPLICAS
    global DICTIONARY


    print('\n----- Resharding -----', file= sys.stderr)

    for repl in REPLICAS:  # verify liveness
        # time.sleep(2)
        URL = 'http://' + repl + '/ping/'
        try:
            requests.get(url=URL, timeout=5)
        except requests.exceptions.ConnectionError:
            REPLICAS.remove(repl)
            delView(repl)

    shardCount = int(request.get_json()['shard-count'])
    if int(shardCount)*2 > len(REPLICAS):
        data = {"message": 'Not enough nodes to provide fault-tolerance with the given shard count!'}
        response = app.response_class(response=json.dumps(
            data), status=400, mimetype='application/json')
        return response

    tempDict={}
    for shard in SHARDS.values():
        for node in shard:
            URL = 'http://' + node + '/request-dict/'
            try:
                print('Requesting dict from',node,file=sys.stderr)
                resp = requests.get(url=URL, timeout=5).json()
                tempDict = {**tempDict,**resp['kvs']}
            except requests.exceptions.ConnectionError:
                delView(node)
    print('Combined Dictionary:',tempDict, file=sys.stderr)

    SHARDS={}
    SHARD_COUNT=shardCount
    print('Shard Count:',SHARD_COUNT,file=sys.stderr)

    #Resharding
    rIndex = 0
    for i in range(1,shardCount+1):
        SHARDS[i] = []
        while len(SHARDS[i]) < 2:
            SHARDS[i].append(REPLICAS[rIndex])
            rIndex += 1
    sIndex = 1
    while rIndex < len(REPLICAS):
        SHARDS[sIndex].append(REPLICAS[rIndex])
        rIndex += 1
        sIndex = max((sIndex+1) % (SHARD_COUNT+1),1)
    print('Resharded Dict:',SHARDS,file=sys.stderr)

    #Updating shards with new view
    broadcastShardOverwrite()
    broadcastClearDict()
    print('All nodes updated with resharded view', file=sys.stderr)

    #Updating dictionaries
    DICTIONARY = {}
    for key,value in tempDict.items():
        data={"value":value[0],
            "version":value[1],
            "causal-metadata":list_to_string(value[2])
            }
        URL='http://'+SOCKET+'/key-value-store/' + key
        requests.put(url=URL, json=data)

    data = {"message":"Resharding done successfully"}
    response = app.response_class(response=json.dumps(data), status=200,mimetype='application/json')
    print('\n----- Resharding Complete -----', file= sys.stderr)
    return response

###################### Shard Receiving ######################
@app.route('/replace-shard-view/', methods=['PUT']) 
def replaceShardView():
    global SHARDS
    global SOCKET
    global SHARD_COUNT
    global current_shard
    global versionlist
    print('Received request to replace shard view with',request.get_json(),file=sys.stderr)
    SHARDS = {}
    requestJson = request.get_json()
    SHARD_COUNT = int(requestJson['shard-count'])
    #versionlist = requestJson[versionlist]
    for key, val in requestJson['shard-dict'].items():
        SHARDS[int(key)] = val
        for node in val:
            if node == SOCKET:
                current_shard = int(key)
    return app.response_class(response=json.dumps(
            {'accepted':'true'}), status=200, mimetype='application/json')

@app.route('/request-dict/', methods=['GET'])
def requestKvs():
    global DICTIONARY
    data = {"kvs": DICTIONARY, "vl": versionlist}
    response = app.response_class(response=json.dumps(
        data), status=200, mimetype='application/json')
    return response

@app.route('/request-shard-view/',methods=['GET'])
def requestShardView():
    global SHARDS
    data = {"shards": SHARDS}
    response = app.response_class(response=json.dumps(
        data), status=200, mimetype='application/json')
    return response    

@app.route('/clear-dict/',methods=['DELETE'])
def clearDict():
    global DICTIONARY
    DICTIONARY = {}
    return app.response_class(response=json.dumps(
        {"accepted":"true"}),status=200,mimetype='application/json')

@app.route('/update-vl/',methods = ['PUT'])
def setVl():
    global versionlist
    responseInJson = request.get_json()
    versionlist = responseInJson['version-list']
    return app.response_class(response=json.dumps(
        {"message":"version list set"}),status=200,mimetype='application/json')
    
def updateAllVl():
    global current_shard
    global SHARDS
    global versionlist
    for i in SHARDS:
        if i != current_shard:
            for node in SHARDS[i]:
                URL = 'http://' + node + '/update-vl'
                response = requests.put(url=URL,json = {'version-list':versionlist})



###################### Shard Helper Functions ######################
def broadcastShardOverwrite():
    global SHARDS
    global SOCKET
    global SHARD_COUNT
    global versionlist
    for node in REPLICAS:
        if node != SOCKET:
            try:
                print('Updating',node,'with new shard view',file=sys.stderr)
                URL = 'http://' + node + '/replace-shard-view/'
                requests.put(url=URL,json={"shard-dict":SHARDS,"vl":versionlist,"shard-count":SHARD_COUNT},timeout=5)
            except requests.exceptions.ConnectionError:
                print(node,'failed to update shard view - deleting node',file=sys.stderr)
                delView(node)

def broadcastClearDict():
    global SHARDS
    global SOCKET
    global versionlist
    for node in REPLICAS:
        if node != SOCKET:
            try:
                print('Clearing',node,'dict',file=sys.stderr)
                URL = 'http://' + node + '/clear-dict/'
                requests.delete(url=URL, timeout=5)
            except requests.exceptions.ConnectionError:
                print(node,'failed to update shard view - deleting node',file=sys.stderr)
                delView(node)

def getShardID(value):
    global SHARD_COUNT
    hash = hashlib.md5()
    hash.update(value.encode('utf-8'))
    return (int(hash.hexdigest(), 16)%SHARD_COUNT + 1)

def getNodesInShard(id):
    global SHARDS
    print(SHARDS, id, file=sys.stderr)
    return SHARDS[id]

def addNodesBalanced(repl):
    global current_shard
    minindex = 0
    minval = 0
    for i in range(1, SHARD_COUNT+1):
        if len(SHARDS[i]) < 2:
            SHARDS[i].append(repl)
            if repl == SOCKET:
                current_shard = i
            return
        else:
            if minval > len(SHARDS[i]):
                minindex = i
                minval = len(SHARDS[i])
    SHARDS[i].append(repl)
    if repl == SOCKET:
        current_shard = i

def removeNodeFromShards(socket):
    global SHARDS
    for shard in SHARDS.values():
        if socket in shard:
            SHARDS[shard].remove(socket)
        return

###################### View Operations ######################
@app.route('/key-value-store-view/', methods=['GET'])
def getView():
    global REPLICAS
    global SOCKET
    # out = REPLICAS #this stores the live replicas

    for repl in REPLICAS:  # verify liveness
        # time.sleep(2)
        URL = 'http://' + repl + '/ping/'
        try:
            requests.get(url=URL, timeout=5)
        except requests.exceptions.ConnectionError:
            REPLICAS.remove(repl)
            delView(repl)
    data = {"message": "View retrieved successfully",
            "view": ",".join(REPLICAS)}  # idk lol
    response = app.response_class(response=json.dumps(
        data), status=200, mimetype='application/json')
    return response


@app.route('/key-value-store-view/', methods=['DELETE'])
def delView(socket=None):
    global REPLICAS
    global SOCKET
    global SHARDS
    if socket == None:
        socket = request.get_json()['socket-address']
    if socket not in REPLICAS:
        data = {"error": "Socket address does not exist in the view",
                "message": "Error in DELETE"}
        response = app.response_class(response=json.dumps(
            data), status=404, mimetype='application/json')
        return response
    for repl in REPLICAS:
        URL = 'http://' + repl + '/view-broadcast-receive/'+socket
        try:
            requests.delete(url=URL)
        except requests.exceptions.ConnectionError:
            print(repl, 'is dead', file=sys.stderr)

    data = {"message": "Replica deleted successfully from the view"}
    response = app.response_class(response=json.dumps(
        data), status=200, mimetype='application/json')
    return response


@app.route('/key-value-store-view/', methods=['PUT'])
def putView():
    global REPLICAS
    global SOCKET
    global DICTIONARY
    socket = request.get_json()['socket-address']
    if socket in REPLICAS:
        data = {"error": "Socket address already exists in the view",
                "message": "Error in PUT"}
        response = app.response_class(response=json.dumps(
            data), status=404, mimetype='application/json')
        return response
    for repl in REPLICAS:
        URL = 'http://' + repl + '/view-broadcast-receive/'+socket
        try:
            # print(url,file=sys.stderr)
            requests.put(url=URL)
            # print(repl,file=sys.stderr)
        except requests.exceptions.ConnectionError:
            print(repl, 'is dead', file=sys.stderr)
    
    data = {"message": "Replica added successfully to the view"}
    response = app.response_class(response=json.dumps(
        data), status=200, mimetype='application/json')
    return response

###################### Receiving view broadcasts ######################
@app.route('/view-broadcast-receive/<socket>', methods=['DELETE'])
def delSelfView(socket):
    global REPLICAS
    global SHARDS
    if socket not in REPLICAS:
        data = {"error": "Socket address does not exist in the view",
                "message": "Error in DELETE"}
        response = app.response_class(response=json.dumps(
            data), status=404, mimetype='application/json')
        return response
    
    REPLICAS.remove(socket)
    removeNodeFromShards(socket)

    #Returning Response
    data = {"message": "Replica deleted successfully from the view"}

    response = app.response_class(response=json.dumps(
        data), status=200, mimetype='application/json')
    return response


@app.route('/view-broadcast-receive/<socket>', methods=['PUT'])
def putSelfView(socket):
    global REPLICAS
    if socket in REPLICAS:
        data = {"error": "Socket address already exists in the view",
                "message": "Error in PUT"}
        response = app.response_class(response=json.dumps(
            data), status=404, mimetype='application/json')
        return response

    REPLICAS.append(socket)

    data = {"message": "Replica successfully added to view"}
    response = app.response_class(response=json.dumps(
        data), status=200, mimetype='application/json')
    return response

######################## Key Value Store Ops ##########################

@app.route('/key-value-store/<key>', methods=['GET'])
def get(key):
    global DICTIONARY
    global versionlist
    global current_shard
    response = ""
    shard_id = getShardID(key)  #determines the shard-id of key
    if shard_id != current_shard:                   #not current node's shard-id
        response = forward_request(key, shard_id)
        return response
    else:
        if key not in DICTIONARY:
            response = broadcast_request(key, shard_id)
            return response
        else:
            keyData = DICTIONARY[key]
            if isinstance(keyData[2], list):
                causal_meta = list_to_string(keyData[2])
            else:
                causal_meta = str(keyData[1])

            data = {"doesExist": True,
                    "message": "Retrieved successfully", "version": str(keyData[1]), "causal-metadata":
                        causal_meta,  "value": keyData[0], "shard-id": shard_id}
            response = app.response_class(response=json.dumps(
                data), status=200, mimetype='application/json')
            return response


@app.route('/key-value-store/<key>', methods=['PUT'])
def put(key):
    global DICTIONARY
    global versionlist
    global REPLICAS
    global SHARD_COUNT
    global current_shard
    response = ""
    value = get_value()
    causal_meta = get_causal_meta()
    shard_id = getShardID(key)  #determines the shard-id of key
    if shard_id != current_shard:         #not current node's shard-id
        print(shard_id,'does not match the current shard of this node,',current_shard,'with shard_count',SHARD_COUNT)
        response = forward_request(key, shard_id)
        return response
    else:
        # Becuase it is empty, the replica knows that the request is not causally dependent
        # on any other PUT operation. Therefore, it generates unique version <V1> for the
        # PUT operation, stores the key, value, the version, and corresponding causal metadata
        # (empty in this case)
        if causal_meta == "" or not versionlist: #If you are putting the first message
            keyData = [value,1,""]  # individual key
            DICTIONARY[key] = keyData

            broadcast_request(key, shard_id)
            versionlist.append(1)
            updateAllVl()
            data = {"message": "Added successfully",
                "version": "1", "causal-metadata": "1", "shard-id": str(shard_id)}
            response = app.response_class(response=json.dumps(
                data), status=201, mimetype='application/json')
            return response
        else:
            checkCausality(causal_meta)
            if key in DICTIONARY:
                message = "Updated successfully"
                status = 200
            else:
                message = "Added successfully"
                status = 201

            keyData = [value, versionlist[-1]+1,versionlist]  # individual key
            DICTIONARY[key] = keyData
            versionlist.append(versionlist[-1]+1)
            updateAllVl()
            broadcast_request(key, shard_id)

            data = {"message": message, "version": str(
                versionlist[-1]), "causal-metadata": list_to_string(versionlist), "shard-id": str(shard_id)}
            response = app.response_class(response=json.dumps(
                data), status=status, mimetype='application/json')
            return response

@app.route('/key-value-store/<key>', methods=['DELETE'])
def delete(key):
    global DICTIONARY
    global versionlist
    global current_shard
    response = ""
    causal_meta = get_causal_meta()
    shard_id = getShardID(key)  #determines the shard-id of key
    if shard_id != current_shard:                   #not current node's shard-id
        response = forward_request(key, shard_id)
        return response
    else:
        checkCausality(causal_meta)
        if key in DICTIONARY:
            keyData = ["",versionlist[-1]+1,versionlist]  # individual key
            DICTIONARY[key] = keyData
            versionlist.append(versionlist[-1]+1)

            broadcast_request(key, shard_id)

            data = {"message": "Deleted successfully", "version": str(
                versionlist[-1]), "causal-metadata": list_to_string(versionlist), "shard-id": current_shard}
            response = app.response_class(response=json.dumps(
                data), status=200, mimetype='application/json')
            return response
        else:
            data = {"DoesExist": False, "error": "key doesn't exist",
                    "message": "error in deleting"}
            response = app.response_class(response=json.dumps(
                data), status=201, mimetype='application/json')
            return response

def checkCausality(causal_meta):
    global versionlist
    if list_to_string(versionlist) != causal_meta:
        time.sleep(0.5)


################## Key Value Store Broadcast Receving ##################
@app.route('/kvs-broadcast-receive/<key>', methods=['GET'])
def broadcastReceiveGet(key):
    global DICTIONARY
    global versionlist
    response = ""
    if key not in DICTIONARY:
        data = {"doesExist": False, "error": "Key does not exist",
                "message": "Error in GET"}
        response = app.response_class(response=json.dumps(
            data), status=404, mimetype='application/json')
        return response

    else:
        keyData = DICTIONARY[key]
        if isinstance(keyData[2], list):
            causal_meta = list_to_string(keyData[2])
        else:
            causal_meta = str(keyData[1])

        data = {"doesExist": True,
                "message": "Retrieved successfully", "version": str(keyData[1]), "causal-metadata":
                    causal_meta,  "value": keyData[0], "shard-id": current_shard}
        response = app.response_class(response=json.dumps(
            data), status=200, mimetype='application/json')
        return response


@app.route('/kvs-broadcast-receive/<key>', methods=['PUT'])
def broadcastReceivePut(key):
    global DICTIONARY
    global versionlist
    response = ""
    value = get_value()
    causal_meta = get_causal_meta()
    print('Received PUT request with JSON:',request.get_json(),file=sys.stderr)
    if not DICTIONARY:
        DICTIONARY = {}
    if causal_meta == "":
        keyData = [value,1,""]  # individual key
        DICTIONARY[key] = keyData
        versionlist.append(1)

        data = {"message": "Replicated successfully", "version": "1"}
        response = app.response_class(response=json.dumps(
            data), status=200, mimetype='application/json')
        return response
    else:
        if versionlist and list_to_string(versionlist) == causal_meta:
            keyData = [value, versionlist[-1]+1,versionlist]  # individual key
            DICTIONARY[key] = keyData
            versionlist.append(versionlist[-1]+1)

            data = {"message": "Replicated successfully",
                    "version": str(versionlist[-1])}
            response = app.response_class(response=json.dumps(
                data), status=200, mimetype='application/json')
            return response
        else:
            l = list_to_string(versionlist)
            data = {"error": " causal metadata not matching",
                    "version": l, "causal_meta": causal_meta}
            response = app.response_class(response=json.dumps(
                data), status=200, mimetype='application/json')
            return response


@app.route('/kvs-broadcast-receive/<key>', methods=['PUT'])
def boradcastReceiveDelete(key):
    global DICTIONARY
    global versionlist
    response = ""
    causal_meta = get_causal_meta()

    if list_to_string(versionlist) == causal_meta:
        if key in DICTIONARY:
            keyData = ["",versionlist[-1]+1,versionlist]  # individual key
            DICTIONARY[key] = keyData
            versionlist.append(versionlist[-1]+1)
            data = {"message": "Deleted successfully", "version": str(
                versionlist[-1]), "causal-metadata": list_to_string(versionlist)}
            response = app.response_class(response=json.dumps(
                data), status=200, mimetype='application/json')
            return response
        else:
            data = {"doesExist": False, "error": "key doesn't exist",
                    "message": "error in deleting"}
            response = app.response_class(response=json.dumps(
                data), status=404, mimetype='application/json')
            return response
    # else
        # wait till previous versions are done

######################### HELPER FUNCTIONS #########################
@app.route('/ping/', methods=['GET'])
def ping():
    return Response("{'Live': 'True'}", status=200, mimetype='application/json')

def list_to_string(_versionlist):
    return ",".join([str(x) for x in _versionlist])

def onStart():
    global REPLICAS
    global SOCKET
    global SHARDS
    global DICTIONARY
    global SHARD_COUNT
    global versionlist
    global current_shard
    if REPLICAS:
        for repl in REPLICAS:
            if repl != SOCKET:
                try:
                    URL = 'http://' + repl + '/key-value-store-view/'
                    request = requests.put(
                        url=URL, json={'socket-address': SOCKET}, timeout=5)

                    URL = 'http://' + repl + '/request-shard-view/'
                    request = requests.get(url=URL, timeout=5).json()
                    for shardId, nodes in request['shards'].items():
                        SHARDS[int(shardId)] = nodes
                    print('Shard view:',SHARDS,'retrieved from',repl,file=sys.stderr)
                    break
                except requests.exceptions.ConnectionError:
                    print(repl, 'failed to respond to view put request', file=sys.stderr)
    if SHARD_COUNT:
        completeStartup()
    else:
        print("Awaiting shard assignment",file=sys.stderr)

@app.route('/assign-shard/<shardid>',methods=['PUT'])
def completeStartup(shardid=None):
    global REPLICAS
    global SOCKET
    global SHARDS
    global DICTIONARY
    global versionlist
    global current_shard

    if shardid:
        current_shard = shardid

    if not SHARDS:
        rIndex = 0
        for i in range(1,SHARD_COUNT+1):
            SHARDS[i] = []
            while len(SHARDS[i]) < 2:
                SHARDS[i].append(REPLICAS[rIndex])
                rIndex += 1
        sIndex = 1
        while rIndex < len(REPLICAS):
            SHARDS[sIndex].append(REPLICAS[rIndex])
            rIndex += 1
            sIndex = max((sIndex+1) % (SHARD_COUNT+1),1)
    for shardId, nodes in SHARDS.items():
        if SOCKET in nodes:
            current_shard = shardId

    for node in getNodesInShard(current_shard):
        try:
            URL = 'http://' + node + '/request-dict/'
            request = requests.get(url=URL, timeout=5).json()
            DICTIONARY = request['kvs']
            versionlist = request['vl']
            break
        except requests.exceptions.ConnectionError:
            print(node,'failed to respond to kvs request',file=sys.stderr)
    
    if DICTIONARY == None:
        DICTIONARY = {}

    print('-------------------------------------',file=sys.stderr)
    print('--- Container Started ---',file=sys.stderr)
    print('Current Shard:', current_shard,file=sys.stderr)
    print('Shard Mapping:',SHARDS, file=sys.stderr)
    print('Dictionary:', DICTIONARY, file=sys.stderr)
    print('-------------------------------------\n',file=sys.stderr)
    
    return Response("{'Live': 'True'}", status=200, mimetype='application/json')

def get_ip(address):
    return address.split(":")[0]


def get_sender_ip():
    return request.remote_addr


def get_value():
    jdict = request.get_json()
    value = jdict["value"]
    return value


def data_has_value():
    if request.get_json()['value']:
        return True
    else:
        return False


def get_causal_meta():
    jdict = request.get_json()
    value = jdict["causal-metadata"]
    return value


def data_has_cm():
    if request.get_json()['causal-metadata']:
        return True
    else:
        return False


def get_curr_version():
    global vectorclock
    return vectorclock[SOCKET]

def forward_request(key, shard_id):
    global SHARDS
    nodes = getNodesInShard(shard_id)
    for repl in nodes:
        URL = 'http://' + repl + '/key-value-store/' + key
        print('Forwarding', request.method, 'for key',key,'to',repl)
        try:
            resp = requests.request(
                method=request.method,
                url=URL,
                headers={key: value for (key, value)
                in request.headers if key != 'Host'},
                data=request.get_data(),
                timeout=5,
                allow_redirects=False)

            excluded_headers = ['content-encoding',
                                'content-length', 'transfer-encoding', 'connection']
            headers = [(name, value) for (name, value) in resp.raw.headers.items()
                       if name.lower() not in excluded_headers]
            
            response = Response(resp.content, resp.status_code, headers)
            return response
        
        # handles error if main instance is not running
        except requests.exceptions.ConnectionError:
            print(repl, 'is dead', file=sys.stderr)
            delView(repl)

def broadcast_request(key, shard_id):
    nodes = getNodesInShard(shard_id)
    print(nodes, file=sys.stderr)
    for repl in nodes:
        if repl != SOCKET:
            print('Broadcasting', request.method, 'for key',key,'to',repl)
            URL = 'http://' + repl + '/kvs-broadcast-receive/' + key
            try:
                resp = requests.request(
                    method=request.method,
                    url=URL,
                    headers={key: value for (key, value)
                             in request.headers if key != 'Host'},
                    data=request.get_data(),
                    timeout=5,
                    allow_redirects=False)

                excluded_headers = ['content-encoding',
                                    'content-length', 'transfer-encoding', 'connection']
                headers = [(name, value) for (name, value) in resp.raw.headers.items()
                           if name.lower() not in excluded_headers]

                response = Response(resp.content, resp.status_code, headers)
                if request.method == "GET":
                    return response

            # handles error if main instance is not running
            except requests.exceptions.ConnectionError:
                print(repl, 'is dead', file=sys.stderr)
                delView(repl)


if __name__ == '__main__':
    onStart()
    app.run(debug=False, host='0.0.0.0', port=8080)
