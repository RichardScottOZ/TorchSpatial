import numpy as np
import json
from scipy import sparse
import torch
import math
import pandas as pd
import os
# from sklearn.neighbors import BallTree, DistanceMetric
from sklearn.metrics import DistanceMetric
from argparse import ArgumentParser
from sklearn.metrics import r2_score, mean_absolute_error, mean_squared_error

from paths import get_paths
import utils as ut
import datasets as dt
import baselines as bl
import models



def compute_acc_batch(
    params,
    val_preds,
    val_classes,
    val_split,
    val_feats=None,
    train_classes=None,
    train_feats=None,
    prior_type="no_prior",
    prior=None,
    hyper_params=None,
    batch_size=1024,
    logger=None,
    eval_flag_str="",
):
    """
    Computes accuracy on held out set with a specified prior. Not very efficient
    as it loops though each example one at a time.
    Args:
        val_preds: CNN pretrained model's image prediction of class [batch_size, num_classes]
            when val_preds = None, we just see the location only prediction accuracy
        val_classes: [batch_size, 1], the list of image category id
        val_split: for bridsnap, np.ones() (batch_size)
        val_feats: the input location features, shape [batch_size, x]
        train_classes:
        train_feats:
        prior_type: 'geo_net'
        prior: the model itself
    Return:
        pred_classes: (batch_size), the list of joint predicted image category
    """
    pred_list = []
    val_classes_list = []
    inds_list = []

    for start_ind in range(0, len(val_classes), batch_size):
        end_ind = min(start_ind + batch_size, len(val_classes))
        inds = np.asarray(list(range(start_ind, end_ind)))
        if val_preds is not None:
            cnn_pred = val_preds[inds, :]
        else:
            cnn_pred = None
            assert prior_type != "no_prior"

        # select the type of prior to be used
        if prior_type == "no_prior":
            pred = cnn_pred

        elif prior_type == "train_freq":
            # prior: (1, num_classes)
            if cnn_pred is not None:
                pred = cnn_pred * prior
            else:
                pred = prior

        elif prior_type == "nn_dist":
            geo_prior = []
            for ind in range(start_ind, end_ind):
                geo_prior.append(
                    bl.compute_neighbor_prior(
                        train_classes,
                        val_preds.shape[1],
                        val_feats[ind, :],
                        prior,
                        hyper_params,
                        ptype="distance",
                    )
                )
            # geo_prior: (batch_size, num_classes)
            geo_prior = np.concatenate(geo_prior, axis=0)
            if cnn_pred is not None:
                pred = cnn_pred * geo_prior
            else:
                pred = geo_prior

        elif prior_type == "nn_knn":
            geo_prior = []
            for ind in range(start_ind, end_ind):
                geo_prior.append(
                    bl.compute_neighbor_prior(
                        train_classes,
                        val_preds.shape[1],
                        val_feats[ind, :],
                        prior,
                        hyper_params,
                        ptype="knn",
                    )
                )
            # geo_prior: (batch_size, num_classes)
            geo_prior = np.concatenate(geo_prior, axis=0)
            if cnn_pred is not None:
                pred = cnn_pred * geo_prior
            else:
                pred = geo_prior

        elif prior_type == "kde":
            geo_prior = []
            for ind in range(start_ind, end_ind):
                geo_prior.append(
                    bl.kde_prior(
                        train_classes,
                        train_feats,
                        val_preds.shape[1],
                        val_locs[ind, :],
                        prior,
                        hyper_params,
                    )
                )
            # geo_prior: (batch_size, num_classes)
            geo_prior = np.concatenate(geo_prior, axis=0)
            if cnn_pred is not None:
                pred = cnn_pred * geo_prior
            else:
                pred = geo_prior

        elif prior_type == "grid":
            geo_prior = prior.eval(val_feats[inds, :])
            if cnn_pred is not None:
                pred = cnn_pred * geo_prior
            else:
                pred = geo_prior

        elif prior_type in ["wrap"] + ut.get_spa_enc_list():
            # if there is no location info won't use prior
            # cnn_pred: the pretrained CNN image class prediction distribution
            # cnn_pred = val_preds[inds, :]
            with torch.no_grad():
                # if all image have location infor
                loc_isnan = (
                    torch.isnan(val_feats[inds, 0]).cpu().data.numpy().astype(int)
                )
                inds = inds[np.where(loc_isnan == 0)]
                # if torch.sum(torch.isnan(val_feats[inds, 0])).item() == 0:
                #     print("Hi!")
                # net_prior: (batch_size, num_classes), the spa_enc model image class prediction distribution
                net_prior = prior(val_feats[inds, :])
                net_prior = net_prior.cpu().data.numpy().astype(np.float64)

                if val_preds is not None:
                    cnn_pred = val_preds[inds, :]
                    # net_prior /= net_prior.sum()  # does not matter for argmax
                    pred = cnn_pred * net_prior
                else:
                    pred = net_prior

                val_classes_list.append(val_classes[inds])

                inds_list.append(inds)

        elif prior_type == "tang_et_al":
            # if there is no location info won't use prior
            # pred = val_preds[ind, :]
            with torch.no_grad():
                loc_isnan = torch.isnan(val_feats[inds, 0]).data.numpy().astype(int)
                inds = inds[np.where(loc_isnan == 0)]
                # if torch.sum(torch.isnan(val_feats[inds, 0])).item() == 0:
                # takes location and network features as input
                pred = prior(
                    val_feats["val_locs"][inds, :], val_feats["val_feats"][inds, :]
                )
                pred = pred.cpu().data.numpy().astype(np.float64)

        # pred_list: (num_batch, batch_size, num_classes)
        pred_list.append(pred)

    # preds: (num_sample, num_classes)
    preds = np.concatenate(pred_list, axis=0)
    # logger.info(preds.shape)

    if prior_type in ["geo_net"] + ut.get_spa_enc_list():
        val_classes_ = np.concatenate(val_classes_list, axis=0)
        logger.info(val_classes_.shape)
        # ranks: np.array(), [batch_size], the rank of the correct class label for each sample, start from 1
        ranks = get_label_rank(loc_pred=preds, loc_class=val_classes_)
        inds_list = np.concatenate(inds_list, axis=0)
    else:
        logger.info(val_classes.shape)
        # ranks: np.array(), [batch_size], the rank of the correct class label for each sample, start from 1
        ranks = get_label_rank(loc_pred=preds, loc_class=val_classes)
        inds_list = None

    top_k_acc = {}
    for kk in [1, 3, 5, 10]:
        top_k_acc[kk] = (ranks <= kk).astype(int)

    if params['save_results'] & (prior_type == "no_prior"):
        pred_classes = []
        predict_results = []
        total_classes = preds.shape[1]

        for ind in range(len(val_classes)):
            pred = preds[ind, :]
            true_class_prob = pred[val_classes[ind]].item()

            pred_classes.append(np.argmax(pred))
            top_N = np.argsort(pred)[-total_classes:]
            true_class_rank = np.where(top_N == val_classes[ind])[0][0] + 1
            sorted_pred_indices = np.argsort(pred)[::-1]
            true_class_index = np.where(sorted_pred_indices == val_classes[ind])[0][0]
            true_class_rank = true_class_index + 1
            reciprocal_rank = 1 / true_class_rank

            hit_at_1 = 1 if true_class_index < 1 else 0
            hit_at_3 = 1 if true_class_index < 3 else 0

            row_result = {
                "true_class_prob": true_class_prob,
                "reciprocal_rank": reciprocal_rank,
                "hit@1": hit_at_1,
                "hit@3": hit_at_3
            }
            predict_results.append(row_result)

        results_df = pd.DataFrame(predict_results)

        # Save the results to a CSV file
        results_csv_path = f"../eval_results/eval_{params['dataset']}_{params['meta_type']}_{params['eval_split']}_no_prior.csv"
        results_df.to_csv(results_csv_path, index=True)

        # Logging the information
        logger.info(f"Save results to {results_csv_path}")

    # print final accuracy
    # some datasets have mutiple splits. These are represented by integers for each example in val_split
    for ii, split in enumerate(np.unique(val_split)):
        logger.info(" Split ID: {}".format(ii))
        inds1 = np.where(val_split == split)[0]
        if inds_list is not None:
            inds2 = sorted(list(set(list(inds1)).intersection(set(list(inds_list)))))
            idx_map = dict(zip(list(inds_list), list(range(len(inds_list)))))
            inds = [idx_map[idx] for idx in inds2]
        else:
            inds = inds1
        for kk in np.sort(list(top_k_acc.keys())):
            logger.info(
                " Top {}\t{}acc (%):   {}".format(
                    kk,
                    eval_flag_str,
                    round(top_k_acc[kk][inds].sum() * 100 / len(inds1), 2),
                )
            )

    pred_classes = list(np.argmax(preds, axis=-1))
    return pred_classes


def compute_regression_acc(params, model, val_feats, val_locs, val_labels,logger):

    model.eval()
    if params['dataset'].startswith('sustainbench'):
        val_feats = val_feats.reshape(val_feats.size(0), 1, 1)
    else:
        pass

    predictions = model(img_feats=val_feats, locs=val_locs)


    # Compute additional metrics
    r2 = r2_score(val_labels.cpu().numpy(), predictions.squeeze().cpu().detach().numpy())
    mae = mean_absolute_error(val_labels.cpu().numpy(), predictions.squeeze().cpu().detach().numpy())
    rmse = mean_squared_error(val_labels.cpu().numpy(), predictions.squeeze().cpu().detach().numpy(), squared=False)

    logger.info("Final regression evaluation results on test:")
    logger.info(f"R2: {r2}")
    logger.info(f"MAE: {mae}")
    logger.info(f"RMSE: {rmse}")

    # Save results to CSV if required
    if params['save_results']:
        epsilon = 1e-8  # Small value to prevent division by zero
        results = pd.DataFrame({
            'lon': val_locs[:, 0].cpu().numpy(),
            'lat': val_locs[:, 1].cpu().numpy(),
            'predictions': predictions.squeeze().cpu().detach().numpy(),
            'labels': val_labels.cpu().numpy(),
            'relative_error': (predictions.squeeze().cpu().detach().numpy() - val_labels.cpu().numpy()) / (val_labels.cpu().numpy()+ epsilon)
        })
        csv_filename = f"../eval_results/eval_{params['dataset']}_{params['meta_type']}_{params['eval_split']}_{params['spa_enc_type']}.csv"
        results.to_csv(csv_filename, index=False)
        print(f"Save results to {csv_filename}")

    return

def get_label_rank(loc_pred, loc_class):
    """
    Args:
        loc_pred: np matrix, [batch_size, num_classes], the prediction probability distribution of each sample over all classes
        loc_class: np matrix, [batch_size], the ground truth class
    """
    loc_pred_ = loc_pred
    # loc_pred_idx: [batch_size, num_classes], the reverse rank (large->small) of all classes based on the probability
    loc_pred_idx = np.argsort(loc_pred_, axis=-1)[:, ::-1]

    # the rank for each class in each sample
    ranks_ = np.argsort(loc_pred_idx, axis=-1) + 1

    loc_class_ = loc_class

    nids = np.arange(loc_pred_.shape[0])

    # rank_list: np.array(), [batch_size], the rank of the correct class label for each sample, start from 1
    rank_list = ranks_[nids, loc_class_]

    # num_classes = loc_pred_.shape[1]

    # loc_class_ = loc_class.cpu().data.numpy()

    # # loc_class_: [batch_size, num_classes], the correct class label for each sample
    # loc_class_ = np.repeat(np.expand_dims(loc_class_, axis = 1), num_classes, axis = 1)

    # # rank_list: np.array(), [batch_size], the rank of the correct class label for each sample, start from 1
    # rank_list = np.argmax(loc_pred_idx == loc_class_, axis = 1) + 1
    return rank_list


def compute_prior(preds, prior_type, prior, train_classes, val_feats, val_preds, hyper_params):
    if prior_type == "no_prior":
        return preds

    elif prior_type == "train_freq":
        return preds * prior

    elif prior_type == "nn_dist":
        geo_prior = bl.compute_neighbor_prior(
            train_classes,
            val_preds.shape[1],
            val_feats,
            prior,
            hyper_params,
            ptype="distance",
        )
        return preds * geo_prior

    elif prior_type == "nn_knn":
        geo_prior = bl.compute_neighbor_prior(
            train_classes,
            val_preds.shape[1],
            val_feats,
            prior,
            hyper_params,
            ptype="knn",
        )
        return preds * geo_prior

    elif prior_type == "kde":
        geo_prior = bl.kde_prior(
            train_classes,
            train_feats,
            val_preds.shape[1],
            val_feats,
            prior,
            hyper_params,
        )
        return preds * geo_prior

    elif prior_type == "grid":
        geo_prior = prior.eval(val_feats)
        return preds * geo_prior

    elif prior_type in ["wrap"] + ut.get_spa_enc_list():
        pred = preds
        with torch.no_grad():
            if torch.isnan(val_feats[0]).item() == 0:
                net_prior = prior(val_feats.unsqueeze(0))
                net_prior = net_prior.cpu().data.numpy()[0, :].astype(np.float64)
                pred = pred * net_prior
        return pred

    elif prior_type == "tang_et_al":
        pred = preds
        with torch.no_grad():
            if torch.isnan(val_feats["val_locs"][0]).item() == 0:
                pred = prior(
                    val_feats["val_locs"].unsqueeze(0),
                    val_feats["val_feats"].unsqueeze(0),
                )
                pred = pred.cpu().data.numpy()[0, :].astype(np.float64)
        return pred


def compute_acc(
    val_preds,
    val_classes,
    val_split,
    val_feats=None,
    train_classes=None,
    train_feats=None,
    prior_type="no_prior",
    prior=None,
    hyper_params=None,
    logger=None,
    eval_flag_str="",
):
    top_k_acc = {}
    for kk in [1, 3, 5, 10]:
        top_k_acc[kk] = np.zeros(len(val_classes))
    max_class = np.max(list(top_k_acc.keys()))
    pred_classes = []

    for ind in range(len(val_classes)):
        pred = compute_prior(
            val_preds[ind, :],
            prior_type,
            prior,
            train_classes,
            val_feats[ind, :],
            val_preds,
            hyper_params,
        )
        pred_classes.append(np.argmax(pred))
        top_N = np.argsort(pred)[-max_class:]
        for kk in top_k_acc.keys():
            if val_classes[ind] in top_N[-kk:]:
                top_k_acc[kk][ind] = 1

    for ii, split in enumerate(np.unique(val_split)):
        logger.info(" Split ID: {}".format(ii))
        inds = np.where(val_split == split)[0]
        for kk in np.sort(list(top_k_acc.keys())):
            logger.info(
                " Top {}\t{}acc (%):   {}".format(
                    kk, eval_flag_str, round(top_k_acc[kk][inds].mean() * 100, 2)
                )
            )

    return pred_classes


def compute_acc_predict_result(
    params,
    val_preds,
    val_classes,
    val_split,
    val_feats=None,
    train_classes=None,
    train_feats=None,
    prior_type="no_prior",
    prior=None,
    hyper_params=None,
    logger=None,
    eval_flag_str="",
):
    top_k_acc = {}
    for kk in [1, 3, 5, 10]:
        top_k_acc[kk] = np.zeros(len(val_classes))
    max_class = np.max(list(top_k_acc.keys()))
    pred_classes = []
    predict_results = []
    total_classes = val_preds.shape[1]

    for ind in range(len(val_classes)):
        pred = compute_prior(
            val_preds[ind, :],
            prior_type,
            prior,
            train_classes,
            val_feats[ind, :],
            val_preds,
            hyper_params,
        )
        true_class_prob = pred[val_classes[ind]].item()

        pred_classes.append(np.argmax(pred))
        top_N = np.argsort(pred)[-total_classes:]
        true_class_rank = np.where(top_N == val_classes[ind])[0][0] + 1
        sorted_pred_indices = np.argsort(pred)[::-1]
        true_class_index = np.where(sorted_pred_indices == val_classes[ind])[0][0]
        true_class_rank = true_class_index + 1
        reciprocal_rank = 1 / true_class_rank

        row_result = {
            "lon": val_feats[ind, 0].item(),
            "lat": val_feats[ind, 1].item(),
            "true_class_prob": true_class_prob,
            "reciprocal_rank": reciprocal_rank,
        }
        predict_results.append(row_result)

        for kk in top_k_acc.keys():
            if val_classes[ind] in top_N[-kk:]:
                top_k_acc[kk][ind] = 1
            if kk in [1, 3]:
                row_result[f"hit@{kk}"] = top_k_acc[kk][ind]

        pred_classes.append(np.argmax(pred))
        top_N = np.argsort(pred)[-max_class:]
        for kk in top_k_acc.keys():
            if val_classes[ind] in top_N[-kk:]:
                top_k_acc[kk][ind] = 1

    results_df = pd.DataFrame(predict_results)
    print(f"Save results to eval_{params['dataset']}_{params['meta_type']}_{params['eval_split']}_{params['spa_enc_type']}.csv")
    results_df.to_csv(f"../eval_results/eval_{params['dataset']}_{params['meta_type']}_{params['eval_split']}_{params['spa_enc_type']}.csv", index=True)

    for ii, split in enumerate(np.unique(val_split)):
        logger.info(" Split ID: {}".format(ii))
        inds = np.where(val_split == split)[0]
        for kk in np.sort(list(top_k_acc.keys())):
            logger.info(
                " Top {}\t{}acc (%):   {}".format(
                    kk, eval_flag_str, round(top_k_acc[kk][inds].mean() * 100, 2)
                )
            )

    return pred_classes


def compute_acc_and_rank(
    val_preds,
    val_classes,
    val_split,
    val_feats=None,
    train_classes=None,
    train_feats=None,
    prior_type="no_prior",
    prior=None,
    hyper_params=None,
    logger=None,
    eval_flag_str="",
):
    """
    Computes accuracy on held out set with a specified prior. Not very efficient
    as it loops though each example one at a time.
    Args:
        val_preds: CNN pretrained model's image prediction of class
        val_classes: [batch_size, 1], the list of image category id
        val_split: for bridsnap, np.ones() (batch_size)
        val_feats: the inpit location features, shape [batch_size, x]
        train_classes:
        train_feats:
        prior_type: 'geo_net'
        prior: the model itself
    Return:
        pred_classes: (batch_size), the list of joint predicted image category
    """

    top_k_acc = {}
    for kk in [1, 3, 5, 10]:
        top_k_acc[kk] = np.zeros(len(val_classes))
    max_class = np.max(list(top_k_acc.keys()))
    pred_classes = []  # the list of joint predicted image category

    pred_list = []

    for ind in range(len(val_classes)):
        # select the type of prior to be used
        if prior_type == "no_prior":
            pred = val_preds[ind, :]

        elif prior_type == "train_freq":
            pred = val_preds[ind, :] * prior

        elif prior_type == "nn_dist":
            geo_prior = bl.compute_neighbor_prior(
                train_classes,
                val_preds.shape[1],
                val_feats[ind, :],
                prior,
                hyper_params,
                ptype="distance",
            )
            pred = val_preds[ind, :] * geo_prior

        elif prior_type == "nn_knn":
            geo_prior = bl.compute_neighbor_prior(
                train_classes,
                val_preds.shape[1],
                val_feats[ind, :],
                prior,
                hyper_params,
                ptype="knn",
            )
            pred = val_preds[ind, :] * geo_prior

        elif prior_type == "kde":
            geo_prior = bl.kde_prior(
                train_classes,
                train_feats,
                val_preds.shape[1],
                val_feats[ind, :],
                prior,
                hyper_params,
            )
            pred = val_preds[ind, :] * geo_prior

        elif prior_type == "grid":
            geo_prior = prior.eval(val_feats[ind, :])
            pred = val_preds[ind, :] * geo_prior

        elif prior_type in ["geo_net"] + ut.get_spa_enc_list():
            # if there is no location info won't use prior
            # pred: the pretrained CNN image class prediction distribution
            pred = val_preds[ind, :]
            with torch.no_grad():
                # if all image have location infor
                if torch.isnan(val_feats[ind, 0]).item() == 0:
                    # net_prior: (1, num_classes), the spa_enc model image class prediction distribution
                    net_prior = prior(val_feats[ind, :].unsqueeze(0))
                    net_prior = net_prior.cpu().data.numpy()[0, :].astype(np.float64)
                    # net_prior /= net_prior.sum()  # does not matter for argmax
                    pred = pred * net_prior

        elif prior_type == "tang_et_al":
            # if there is no location info won't use prior
            pred = val_preds[ind, :]
            with torch.no_grad():
                if torch.isnan(val_feats["val_locs"][ind, 0]).item() == 0:
                    # takes location and network features as input
                    pred = prior(
                        val_feats["val_locs"][ind, :].unsqueeze(0),
                        val_feats["val_feats"][ind, :].unsqueeze(0),
                    )
                    pred = pred.cpu().data.numpy()[0, :].astype(np.float64)

        # store accuracy of prediction
        pred_classes.append(np.argmax(pred))
        top_N = np.argsort(pred)[-max_class:]
        for kk in top_k_acc.keys():
            if val_classes[ind] in top_N[-kk:]:
                top_k_acc[kk][ind] = 1

        pred_list.append(np.expand_dims(pred, axis=0))

    # print final accuracy
    # some datasets have mutiple splits. These are represented by integers for each example in val_split
    for ii, split in enumerate(np.unique(val_split)):
        print(" Split ID: {}".format(ii))
        inds = np.where(val_split == split)[0]
        for kk in np.sort(list(top_k_acc.keys())):
            print(
                " Top {}\tacc (%):   {}".format(
                    kk, round(top_k_acc[kk][inds].mean() * 100, 2)
                )
            )

    # preds: (num_sample, num_classes)
    preds = np.concatenate(pred_list, axis=0)

    # ranks: np.array(), [batch_size], the rank of the correct class label for each sample, start from 1
    ranks = get_label_rank(loc_pred=preds, loc_class=val_classes)

    top_k_acc = {}
    for kk in [1, 3, 5, 10]:
        top_k_acc[kk] = (ranks <= kk).astype(int)

    # print final accuracy
    # some datasets have mutiple splits. These are represented by integers for each example in val_split
    for ii, split in enumerate(np.unique(val_split)):
        logger.info(" Split ID: {}".format(ii))
        inds = np.where(val_split == split)[0]
        for kk in np.sort(list(top_k_acc.keys())):
            logger.info(
                " Top {}\t{}acc (%):   {}".format(
                    kk, eval_flag_str, round(top_k_acc[kk][inds].mean() * 100, 2)
                )
            )

    return pred_classes, ranks


def get_cross_val_hyper_params(eval_params):
    hyper_params = {}
    if eval_params["dataset"] == "inat_2018":
        hyper_params["num_neighbors"] = 1500
        hyper_params["dist_type"] = "euclidean"  # euclidean, haversine
        hyper_params["dist_thresh"] = 2.0  # kms if haversine - divide by radius earth
        hyper_params["gp_size"] = [180, 60]
        hyper_params["pseudo_count"] = 2
        hyper_params["kde_dist_type"] = "euclidean"  # for KDE
        hyper_params["kde_quant"] = 5.0  # for KDE
        hyper_params["kde_nb"] = 700  # for KDE

    elif eval_params["dataset"] == "inat_2017":
        hyper_params["num_neighbors"] = 1450
        hyper_params["dist_type"] = "euclidean"
        hyper_params["dist_thresh"] = 5.0
        hyper_params["gp_size"] = [45, 30]
        hyper_params["pseudo_count"] = 2
        hyper_params["kde_dist_type"] = "euclidean"
        hyper_params["kde_quant"] = 5.0
        hyper_params["kde_nb"] = 700

    elif (
        eval_params["dataset"] == "birdsnap"
        and eval_params["meta_type"] == "ebird_meta"
    ):
        hyper_params["num_neighbors"] = 700
        hyper_params["dist_type"] = "euclidean"
        hyper_params["dist_thresh"] = 5.0
        hyper_params["gp_size"] = [30, 30]
        hyper_params["pseudo_count"] = 2
        hyper_params["kde_dist_type"] = "euclidean"
        hyper_params["kde_quant"] = 0.001
        hyper_params["kde_nb"] = 500

    elif (
        eval_params["dataset"] == "birdsnap" and eval_params["meta_type"] == "orig_meta"
    ):
        hyper_params["num_neighbors"] = 100
        hyper_params["dist_type"] = "euclidean"
        hyper_params["dist_thresh"] = 9.0
        hyper_params["gp_size"] = [225, 60]
        hyper_params["pseudo_count"] = 2
        hyper_params["kde_dist_type"] = "euclidean"
        hyper_params["kde_quant"] = 0.001
        hyper_params["kde_nb"] = 600

    elif eval_params["dataset"] == "nabirds":
        hyper_params["num_neighbors"] = 500
        hyper_params["dist_type"] = "euclidean"
        hyper_params["dist_thresh"] = 6.0
        hyper_params["gp_size"] = [45, 60]
        hyper_params["pseudo_count"] = 2
        hyper_params["kde_dist_type"] = "euclidean"
        hyper_params["kde_quant"] = 0.001
        hyper_params["kde_nb"] = 600

    elif eval_params["dataset"] == "yfcc":
        hyper_params["num_neighbors"] = 75
        hyper_params["dist_type"] = "haversine"
        hyper_params["dist_thresh"] = 2.0 / 6371.4
        hyper_params["gp_size"] = [540, 150]
        hyper_params["pseudo_count"] = 3
        hyper_params["kde_dist_type"] = "euclidean"
        hyper_params["kde_quant"] = 0.001
        hyper_params["kde_nb"] = 300

    return hyper_params
