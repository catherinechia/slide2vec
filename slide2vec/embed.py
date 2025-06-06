import os
import tqdm
import h5py
import glob
import torch
import shutil
import argparse
import traceback
import torchvision
import pandas as pd
import multiprocessing as mp

from pathlib import Path
from contextlib import nullcontext

import slide2vec.distributed as distributed

from slide2vec.utils import fix_random_seeds
from slide2vec.utils.config import get_cfg_from_file, setup_distributed
from slide2vec.models import ModelFactory
from slide2vec.data import TileDataset, RegionUnfolding


def get_args_parser(add_help: bool = True):
    parser = argparse.ArgumentParser("slide2vec", add_help=add_help)
    parser.add_argument(
        "--config-file", default="", metavar="FILE", help="path to config file"
    )
    parser.add_argument(
        "--run-id",
        type=str,
        help="Name of output directory",
    )
    return parser


def create_transforms(cfg, model):
    if cfg.model.level in ["tile", "slide"]:
        return model.get_transforms()
    elif cfg.model.level == "region":
        return torchvision.transforms.Compose(
            [
                torchvision.transforms.ToTensor(),
                RegionUnfolding(model.tile_size),
                model.get_transforms(),
            ]
        )
    else:
        raise ValueError(f"Unknown model level: {cfg.model.level}")


def create_dataset(wsi_fp, coordinates_dir, cfg, transforms):
    return TileDataset(
        wsi_fp,
        coordinates_dir,
        cfg.tiling.params.spacing,
        backend=cfg.tiling.backend,
        transforms=transforms,
    )


def deduplicate_features(indices_all, wsi_feature):
    """
    Deduplicates the features tensor based on the indices.
    Returns both the deduplicated features and the sorted unique indices.
    """
    sorted_order = indices_all.argsort()
    indices_sorted = indices_all[sorted_order]
    features_sorted = wsi_feature[sorted_order]

    dedup_dict = {}
    for i, idx in enumerate(indices_sorted):
        if idx.item() not in dedup_dict:
            dedup_dict[idx.item()] = features_sorted[i]
    unique_idxs = sorted(dedup_dict.keys())
    dedup_features = torch.stack([dedup_dict[k] for k in unique_idxs], dim=0)
    return dedup_features, unique_idxs


def run_inference(features_dir, tmp_dir, dataloader, model, device, autocast_context, unit, batch_size):
    """
    Run inference on the provided dataloader and return a temporary dir unique to filename and rank.
    """
    # Infer total samples per rank (roughly), assuming the dataset is evenly split among ranks
    try:
        total_samples = len(dataloader.dataset) // distributed.get_local_size() #get_global_size()?
    except Exception:
        raise ValueError("Could not determine dataset length.")
    
    # Get feature shape using a dry run
    model.eval()
    with torch.inference_mode(), autocast_context:
        sample_batch = next(iter(dataloader))
        sample_input = sample_batch[1].to(device)
        sample_output = model(sample_input)
        feature_shape = sample_output.shape[1:]  # (C,) or (C, H, W)

    #print("Before inference ", torch.cuda.memory_allocated() / 1024**2, "MB allocated")
    
    # Create HDF5 datasets
    with torch.inference_mode(), autocast_context:
        for batch in tqdm.tqdm(
            dataloader,
            desc=f"Inference on GPU {distributed.get_local_rank()}",
            unit=unit,
            unit_scale=batch_size,
            leave=False,
            position=2 + distributed.get_local_rank(),
        ):
            #print("Inside for batch in tqdm ", distributed.get_local_rank(), " rank")
            hdf5_path = os.path.join(tmp_dir, f"features_rank{distributed.get_local_rank()}.h5") #get_global_rank() instead of get_local_rank()?
            #print("HDF5 path: ", hdf5_path)
            
            idx, image = batch
            batch_size = image.size(0)
            image = image.to(device, non_blocking=True)

            with torch.inference_mode(), autocast_context:
                feature = model(image).cpu()

            # Check if hdf5 file already exists, if not create it, if yes, append to it
            if not os.path.exists(hdf5_path):
                offset = 0
                # Create HDF5 file and datasets
                with h5py.File(hdf5_path, "w") as f:
                    features_dset = f.create_dataset("features", shape=(total_samples, *feature_shape))
                    indices_dset = f.create_dataset("indices", shape=(total_samples,))
                    #
                    # Write to HDF5
                    features_dset[offset:offset+batch_size] = feature
                    indices_dset[offset:offset+batch_size] = idx
                    offset += batch_size
            else:
                # Append to existing HDF5 file
                with h5py.File(hdf5_path, "a") as f:
                    features_dset = f["features"]
                    indices_dset = f["indices"]
                    #
                    # Write to HDF5
                    features_dset[offset:offset+batch_size] = feature
                    indices_dset[offset:offset+batch_size] = idx
                    offset += batch_size
            # Clear memory
            del image, feature, idx
            torch.cuda.empty_cache()
    return None

def load_all_features(tmp_dir):
    features_list = []
    indices_list = []

    for path in sorted(glob.glob(os.path.join(tmp_dir, "features_rank*.h5"))):
        with h5py.File(path, "r") as f:
            features_list.append(torch.from_numpy(f["features"][:]))
            indices_list.append(torch.from_numpy(f["indices"][:]))
    
    # Remove all the files in tmp_dir
    for path in sorted(glob.glob(os.path.join(tmp_dir, "features_rank*.h5"))):
        os.remove(path)

    return torch.cat(features_list, dim=0), torch.cat(indices_list, dim=0)

# from torch.distributed.elastic.multiprocessing.errors import record
# @record
def main(args):
    # setup configuration
    cfg = get_cfg_from_file(args.config_file)
    output_dir = Path(cfg.output_dir, args.run_id)
    cfg.output_dir = str(output_dir)

    setup_distributed()

    coordinates_dir = Path(cfg.output_dir, "coordinates")
    fix_random_seeds(cfg.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    num_workers = min(mp.cpu_count(), cfg.speed.num_workers_embedding)
    if "SLURM_JOB_CPUS_PER_NODE" in os.environ:
        num_workers = min(num_workers, int(os.environ["SLURM_JOB_CPUS_PER_NODE"]))

    process_list = Path(cfg.output_dir, "process_list.csv")
    assert (
        process_list.is_file()
    ), "Process list CSV not found. Ensure tiling has been run."
    process_df = pd.read_csv(process_list)
    skip_feature_extraction = process_df["feature_status"].str.contains("success").all()

    if skip_feature_extraction and distributed.is_main_process():
        print("Feature extraction already completed.")
        return

    model = ModelFactory(cfg.model).get_model()
    if distributed.is_main_process():
        print("Starting feature extraction...")
    torch.distributed.barrier()

    # select slides that were successfully tiled but not yet processed for feature extraction
    sub_process_df = process_df[process_df.tiling_status == "success"]
    mask = sub_process_df["feature_status"] != "success"
    process_stack = sub_process_df[mask]
    total = len(process_stack)
    wsi_paths_to_process = [Path(x) for x in process_stack.wsi_path.values.tolist()]

    features_dir = Path(cfg.output_dir, "features")
    if distributed.is_main_process():
        features_dir.mkdir(exist_ok=True, parents=True)

    autocast_context = (
        torch.autocast(device_type="cuda", dtype=torch.float16)
        if cfg.speed.fp16
        else nullcontext()
    )
    unit = "tile" if cfg.model.level != "region" else "region"
    feature_extraction_updates = {}

    transforms = create_transforms(cfg, model)
    print(f"transform: {transforms}")

    for wsi_fp in tqdm.tqdm(
        wsi_paths_to_process,
        desc="Inference",
        unit="slide",
        total=total,
        leave=True,
        disable=not distributed.is_main_process(),
        position=1,
    ):
        try:
            dataset = create_dataset(wsi_fp, coordinates_dir, cfg, transforms)
            if distributed.is_enabled_and_multiple_gpus():
                sampler = torch.utils.data.DistributedSampler(
                    dataset,
                    shuffle=False,
                    drop_last=False,
                )
            else:
                sampler = None
            dataloader = torch.utils.data.DataLoader(
                dataset,
                batch_size=cfg.model.batch_size,
                sampler=sampler,
                num_workers=num_workers,
                pin_memory=True,
            )
            # Temporary directory
            # Get the filename without extension from wsi_fp 
            wsi_filename = os.path.splitext(os.path.basename(wsi_fp))[0]
            tmp_dir = Path(features_dir, "tmp", wsi_filename)
            os.makedirs(tmp_dir, exist_ok=True)

            run_inference(
                features_dir, #Added to store temporary features chunks
                tmp_dir,
                dataloader,
                model,
                model.device,
                autocast_context,
                unit,
                cfg.model.batch_size,
            )

            features, indices = load_all_features(tmp_dir)

            # gather features from all gpus if needed
            if distributed.is_enabled_and_multiple_gpus():
                features_list = distributed.gather_tensor(features)
                indices_list = distributed.gather_tensor(indices)
                if distributed.is_main_process():
                    wsi_feature = torch.cat(features_list, dim=0)
                    indices_all = torch.cat(indices_list, dim=0)
                else:
                    # For non-main processes, a placeholder is provided.
                    wsi_feature = torch.rand(
                        (len(dataset), model.features_dim), device=model.device
                    )
                    indices_all = None
            else:
                wsi_feature = features
                indices_all = indices

            if distributed.is_main_process():
                wsi_feature, unique_idxs = deduplicate_features(
                    indices_all, wsi_feature
                )

            torch.distributed.barrier()

            # for slide-level models, align coordinates with feature order
            # then run forward pass with slide encoder
            if cfg.model.level == "slide":
                if distributed.is_main_process():
                    if cfg.model.name == "prov-gigapath":
                        coordinates = torch.tensor(
                            dataset.scaled_coordinates[unique_idxs],
                            dtype=torch.int64,
                            device=model.device,
                        )
                    else:
                        coordinates = torch.tensor(
                            dataset.coordinates[unique_idxs],
                            dtype=torch.int64,
                            device=model.device,
                        )
                else:
                    coordinates = torch.randint(
                        10000,
                        (len(dataset), 2),
                        dtype=torch.int64,
                        device=model.device,
                    )
                with torch.inference_mode():
                    with autocast_context:
                        wsi_feature = model.forward_slide(
                            wsi_feature,
                            tile_coordinates=coordinates,
                            tile_size_lv0=dataset.tile_size_lv0,
                        )

            if distributed.is_main_process():
                torch.save(wsi_feature, Path(features_dir, f"{wsi_fp.stem}.pt"))

            feature_extraction_updates[str(wsi_fp)] = {"status": "success"}

        except Exception as e:
            feature_extraction_updates[str(wsi_fp)] = {
                "status": "failed",
                "error": str(e),
                "traceback": str(traceback.format_exc()),
            }

        # update process_df
        if distributed.is_main_process():
            status_info = feature_extraction_updates[str(wsi_fp)]
            process_df.loc[
                process_df["wsi_path"] == str(wsi_fp), "feature_status"
            ] = status_info["status"]
            if "error" in status_info:
                process_df.loc[
                    process_df["wsi_path"] == str(wsi_fp), "error"
                ] = status_info["error"]
                process_df.loc[
                    process_df["wsi_path"] == str(wsi_fp), "traceback"
                ] = status_info["traceback"]
            process_df.to_csv(process_list, index=False)

    if distributed.is_enabled_and_multiple_gpus():
        torch.distributed.barrier()

    if distributed.is_main_process():
        # summary logging
        slides_with_tiles = len(sub_process_df)
        total_slides = len(process_df)
        failed_feature_extraction = process_df[
            ~(process_df["feature_status"] == "success")
        ]
        print("=+=" * 10)
        print(f"Total number of slides with tiles: {slides_with_tiles}/{total_slides}")
        print(f"Failed feature extraction: {len(failed_feature_extraction)}")
        print(
            f"Completed feature extraction: {total_slides - len(failed_feature_extraction)}"
        )
        print("=+=" * 10)
    
    #Remove the temporary directory
    tmp_dir_super = Path(features_dir, "tmp")
    shutil.rmtree(tmp_dir_super, ignore_errors=True)


if __name__ == "__main__":
    args = get_args_parser(add_help=True).parse_args()
    main(args)
