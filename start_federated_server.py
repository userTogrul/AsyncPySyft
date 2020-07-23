#-----------------------------------------------------------------------------------------------#
#                                                                                               #
#   I M P O R T     G L O B A L     L I B R A R I E S                                           #
#                                                                                               #
#-----------------------------------------------------------------------------------------------#
import asyncio
import websockets
import argparse
import time

import torch
import torch.nn as nn
import torchvision

import syft as sy
# this hook is needed before the training_plan library import
hook = sy.TorchHook(torch)

#-----------------------------------------------------------------------------------------------#
#                                                                                               #
#   I M P O R T     L O C A L     L I B R A R I E S   /   F I L E S                             #
#                                                                                               #
#-----------------------------------------------------------------------------------------------#
from workers import FederatedServer
from modules.model_loader import get_model
from modules.data_loader import load_dataset
from modules.validate import validate
from modules.training_plan import build_and_get_train_plan, set_model_params
from utils.utils import average_model_parameters
from configs import globals as glb

#-----------------------------------------------------------------------------------------------#
#                                                                                               #
#   Define global parameters.                                                                   #
#                                                                                               #
#-----------------------------------------------------------------------------------------------#
WORKER_LIST = []
    
#***********************************************************************************************#
#                                                                                               #
#   description:                                                                                #
#   helper fucntions to communicate with client worker and their information.                   #
#                                                                                               #
#***********************************************************************************************#
async def connection_handler(websocket, path):
    # receive information of the worker
    worker_id = await websocket.recv()
    worker_host = await websocket.recv()
    worker_port = await websocket.recv()
    # print log message
    print("connection received from client {0}!!!!".format(worker_id))
    # setup arguments
    kwargs_websocket = {"host": worker_host, "hook": hook, "verbose": True}
    time.sleep(5)
    # create new instance of the websocket server object
    remote_client = FederatedServer(id=worker_id, port=int(worker_port), **kwargs_websocket)
    # update the local dictionary
    WORKER_LIST.append([remote_client, worker_id, worker_host, int(worker_port)])

#***********************************************************************************************#
#                                                                                               #
#   description:                                                                                #
#   helper fucntions to communicate with client worker.                                         #
#                                                                                               #
#***********************************************************************************************#
async def fit_model_on_worker(worker: FederatedServer, model_params, train_plan, dataset_key, epoch, kwargs):
    """Send the model to the worker and fit the model on the worker's training data.
    Args:
        worker: Remote location, where the model shall be trained.
        model: Batch size of each training step.
        train_plan: Model which shall be trained.
        epoch: current epoch being run
    Returns:
        A tuple containing:
            * worker_id: Union[int, str], id of the worker.
            * updated_parameter: parameters of the improved model.
            * loss: Loss on last training batch, torch.tensor.
    """
    # clear all remote objects
    worker.clear_objects_remote()
    
    model_ptrs = []
    # send the model parameters to the worker
    for idx, item in enumerate(model_params):
        item.id = kwargs["model_param_id"]+"_"+worker.id+"_{0}".format(idx)
        model_ptrs.append(item.send(worker))
    kwargs["model_tensor_count"] = len(model_params)
    
    # set train configurations on the remote worker
    await worker.set_train_config(**kwargs)
    
    # send a copy of training plan to respective worker
    copy_plan = train_plan.copy()
    copy_plan.id = train_plan.id
    copy_plan.send(worker)
    
    # run the async fit method
    task_object = worker.async_fit(dataset_key=dataset_key, epoch=epoch, return_ids=["loss", "model_param"])
    loss = await task_object        

    # fetch new model
    updated_parameter = []
    for ptr in model_ptrs:
        updated_parameter.append(ptr.get())
    
    # return results    
    return worker.id, loss, updated_parameter #model, loss

#***********************************************************************************************#
#                                                                                               #
#   description:                                                                                #
#   helper fucntions to build arguments dictionary for training configurations.                 #
#                                                                                               #
#***********************************************************************************************#
def build_training_configurations():
    # create an arguments dictionary
    kwargs = dict()
    kwargs["plan_id"] = glb.PLAN_ID
    kwargs["model_id"] = glb.MODEL
    kwargs["model_param_id"] = glb.MODEL_PARAM_ID
    kwargs["lr"] = glb.INITIAL_LR
    kwargs["batch_size"] = glb.BATCH_SIZE
    kwargs["random_sample"] = glb.RANDOM_SAMPLE_BATCHES
    kwargs["max_nr_batches"] = glb.MAX_NR_BATCHES
    kwargs["dataset_key"] = glb.DATASET_ID
    kwargs["epochs"] = glb.NUM_EPOCHS
    return kwargs

#***********************************************************************************************#
#                                                                                               #
#   description:                                                                                #
#   helper fucntions to communicate with client worker.                                         #
#                                                                                               #
#***********************************************************************************************#
async def training_handler():
    # yield control to connector class
    await asyncio.sleep(30)
    
    # build and get the train plan
    train_plan  = build_and_get_train_plan()
    
    # create training arguments
    kwargs = build_training_configurations()
    
    # build model
    model = get_model(model_name=glb.MODEL)
    
    # get a loss function
    criterion = nn.CrossEntropyLoss()
    
    # load test dataset
    _, test_loader = load_dataset(dataset=glb.DATASET, loaders=True)
    
    # get some variable
    epochs = glb.NUM_EPOCHS
    
    # iterate over the workers
    for epoch in range(epochs):
        # print log message
        print("Running epoch {0} of {1}".format(epoch+1, epochs))
            
        # sample workers based on our logic here
        sampled_workers = [worker[0] for worker in WORKER_LIST] #[WORKER_LIST[0][0]]
        print("SAMPLED WORKER COUNT: ", len(sampled_workers))
        
        # extract latest model parameters
        model_params = [param.data for param in model.parameters()]
        
        # run the training on all workers
        results = await asyncio.gather(
            *[
                fit_model_on_worker(
                    worker=worker,
                    model_params=model_params,
                    train_plan=train_plan,
                    dataset_key=glb.DATASET_ID,
                    epoch=epoch,
                    kwargs=kwargs,
                )
                for worker in sampled_workers
            ])
        
        # extract from all workers the updated model parameters
        upd_wrkr_params = {}
        for worker_id, worker_loss, worker_model in results:
            upd_wrkr_params[worker_id] = worker_model
        
        # get the parameter average
        param_avg = average_model_parameters(upd_wrkr_params)
        
        # unpack the new parameters into local model
        set_model_params(model, param_avg)
        
        # evaluate on testset using the new model
        print("Begin Validation @ Epoch {}".format(epoch+1))
        val_loss, prec1 = validate(test_loader, model, criterion)
        
    while True:
        continue

#***********************************************************************************************#
#                                                                                               #
#   description:                                                                                #
#   argument parsing and configurations for setting up the federated server.                    #
#                                                                                               #
#***********************************************************************************************#
if __name__ == '__main__':
    # parse the arguments
    parser = argparse.ArgumentParser(description="Setup Federated Server Module.")
    parser.add_argument("--port", type=int, help="port number of federated server, e.g. --port 8778", required=True)
    parser.add_argument("--host", type=str, default="localhost", help="host for the connection")
    args = parser.parse_args()
    
    # listen on the listen_port to connect new client
    start_server = websockets.serve(connection_handler, args.host, args.port)
    
    # run forever
    print("REACHED THIS POINT, NOW WAITING FOR WORKERS")
    
    # create a forever running event loop
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.ensure_future(training_handler())
    asyncio.get_event_loop().run_forever()
