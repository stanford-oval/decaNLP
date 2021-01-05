from typing import List
import xgboost as xgb
import numpy as np
import sklearn
import pickle
from os import path
import torch
import itertools
from sklearn.metrics import accuracy_score, confusion_matrix, precision_recall_curve
from matplotlib import pyplot
import argparse

def pad_features(confidences, f, normalize='var'):
    pad_len = max([len(f(c)) for c in confidences])
    all_features = []
    all_lengths = []
    for c in confidences:
        features = f(c)
        # print(features)
        all_lengths.append(len(features))
        all_features.append(np.pad(features, pad_width=(0, pad_len-len(features)), constant_values=np.nan, mode='constant'))

    all_features = np.stack(all_features)
    if normalize == 'var':
        mean = np.nanmean(all_features, axis=0)
        var = np.nanvar(all_features, axis=0)
        all_features = (all_features - mean) / np.sqrt(var)
    elif normalize == 'max':
        _max = np.max(all_features, axis=0)
        _min = np.min(all_features, axis=0)
        all_features = (all_features - _min) / (_max-_min)
    elif normalize == 'none':
        pass
    else:
        raise ValueError('Unexpected value for `normalize`')
    all_features[np.isnan(all_features)] = 0
    # print('all_features = ', all_features)
    
    return all_features, all_lengths

def interleave_features(features: List[np.array]):
    assert len(features) == 2
    a = features[0]
    b = features[1]
    assert a.shape == b.shape
    interleaved = np.empty((a.shape[0], a.shape[1] + b.shape[1]), dtype=a.dtype)
    interleaved[:, 0::2] = a
    interleaved[:, 1::2] = b
    # print('a = ', a)
    # print('b = ', b)
    # print('interleaved = ', interleaved)
    return interleaved

def logit_cv_0(x):
    # return torch.cat([torch.max(x[0].logit_cv).view(-1), torch.max(x[0].logit_variance).view(-1)], dim=0)
    return x[0].logit_cv

def logit_cv_1(x):
    return x[1].logit_cv

def max_var_0(x):
    return x[0].logit_variance.max().view(-1)

def logit_mean_0(x):
    return x[0].logit_mean

def nodrop_entropies_0(x):
    return x[0].nodrop_entropies

def nodroplogit_0(x):
    return x[0].nodrop_logits

def logit_mean_1(x):
    return x[1].logit_mean

def logit_var_0(x):
    return x[0].logit_variance

def avg_logprob(x):
    return torch.mean(x[0].nodrop_logits).item()

def length_0(x):
    return torch.tensor(len(x[0].logit_mean)).view(-1)

def tune_and_train(train_dataset, dev_dataset, dev_labels, scale_pos_weight):
    max_depth = [3, 5, 7, 10, 20, 30, 50] # the maximum depth of each tree
    eta = [0.02, 0.1, 0.5, 0.7] # the training step for each iteration
    num_round = [300]

    best_score = 0
    best_model = None
    best_confusion_matrix = None
    best_params = None
    for m, e, n in itertools.product(max_depth, eta, num_round):
        params = {
            'max_depth': m,  
            'eta': e,  
            'objective': 'binary:logistic',
            'eval_metric': 'aucpr',
            'scale_pos_weight': scale_pos_weight
            }
        evals_result = {}
        model = xgb.train(params=params,
                          dtrain=train_dataset,
                          evals=[(dev_dataset, 'dev')],
                          num_boost_round=n, 
                          early_stopping_rounds=50,
                          evals_result=evals_result,
                          verbose_eval=False)
        # print('evals_result = ', evals_result)
        prediction_probs = extract_confidence_scores(model, dev_dataset)
        predictions = np.round(np.asarray(prediction_probs))
        accuracy = accuracy_score(dev_labels, predictions)
        score = model.best_score #evals_result['dev']['aucpr'][-1]#
        print('score=%.1f \t accuracy=%.1f \t best_iteration=%d \t' % (score * 100, accuracy * 100, model.best_iteration))
        confusion_m = confusion_matrix(dev_labels, predictions)
        # print('max_depth = ' + str(m) + ' eta = ' + str(e) + ' num_round = ' + str(n))
        if score > best_score:
            best_score = score
            best_model = model
            best_confusion_matrix = confusion_m
            best_params = m, e, n
        best_score = max(best_score, score)

    return best_model, best_score, best_confusion_matrix, best_params


def extract_confidence_scores(model, dev_dataset):
    prediction_probs = model.predict(dev_dataset, ntree_limit=model.best_ntree_limit)
    return prediction_probs


def run(confidences, featurizers):
    all_labels = []
    for c in confidences:
        all_labels.append(c[0].first_mistake)
    
    all_features = []
    for featurizer in featurizers:
        if isinstance(featurizer, tuple):
            fs = []
            for f in featurizer:
                p, _ = pad_features(confidences, f)
                fs.append(p)
            padded_feature = interleave_features(fs)
        else:
            padded_feature, _ = pad_features(confidences, featurizer)
        all_features.append(padded_feature)
        # print('padded_feature = ', padded_feature[:,-1].nonzero())
    all_features = np.concatenate(all_features, axis=1)
    # print('all_features = ', all_features.shape)

    all_labels = np.array(all_labels) + 1 # +1 so that minimum is 0
    all_labels = (all_labels == 0)
    # print('all_labels = ', all_labels)
    # avg_logprobs = [avg_logprob(c) for c in confidences]
    all_features_train, all_features_dev, all_labels_train, all_labels_dev = \
        sklearn.model_selection.train_test_split(all_features, all_labels, test_size=0.2, random_state=123)
    dtrain = xgb.DMatrix(data=all_features_train, label=all_labels_train)
    ddev = xgb.DMatrix(data=all_features_dev, label=all_labels_dev)
    # print('ratio of 1s in test set = ', np.sum(all_labels_dev)/len(all_labels_dev))
    scale_pos_weight = np.sum(all_labels_dev)/(np.sum(1-all_labels_dev)) # 1s over 0s
    # print('scale_pos_weight = ', scale_pos_weight)

    best_model, best_score, best_confusion_matrix, best_params = tune_and_train(train_dataset=dtrain, dev_dataset=ddev, dev_labels=all_labels_dev, scale_pos_weight=scale_pos_weight)
    print('best dev set score = %.1f' % (best_score * 100))
    print('best confusion_matrix = ', best_confusion_matrix)
    print('best hyperparameters (max_depth, eta, n) = ', best_params)
    print('-'*10)

    confidence_scores = extract_confidence_scores(best_model, ddev)

    order = range(len(all_labels_dev))
    sorted_confidence_scores, sorted_labels, original_order = list(zip(*sorted(zip(confidence_scores, all_labels_dev, order))))
    sorted_features = [all_features_dev[i] for i in original_order]
    # print('sorted_features = ', sorted_features[-6:-4])
    # print('sorted_confidence_scores = ',  sorted_confidence_scores[-6:-4])
    # print('sorted_confidence_scores = ',  sorted_confidence_scores)
    # print('sorted_labels = ', sorted_labels[-6:-4])

    
    precision, recall, thresholds = precision_recall_curve(all_labels_dev, confidence_scores)
    pass_rate, accuracies = accuracy_at_pass_rate(all_labels_dev, confidence_scores)

    return precision, recall, pass_rate, accuracies, thresholds


def accuracy_at_pass_rate(labels, confidence_scores):
    sorted_confidence_scores, sorted_labels = zip(*sorted(zip(confidence_scores, labels)))
    sorted_labels = np.array(sorted_labels, dtype=np.int)
    # print('sorted_confidence_scores = ', sorted_confidence_scores)
    # print('sorted_labels = ', sorted_labels)
    all_pass_rates = []
    all_accuracies = []
    for i in range(len(sorted_labels)):
        pass_labels = sorted_labels[i:]
        pass_rate = len(pass_labels) / len(sorted_labels)
        all_pass_rates.append(pass_rate)
        accuracy = np.sum(pass_labels) / len(pass_labels)
        all_accuracies.append(accuracy)

    return all_pass_rates, all_accuracies


def parse_argv(parser):
    parser.add_argument('--confidence_path', type=str, help='The path to the pickle file where the list of ConfidenceOutput objects is saved')
    # parser.add_argument('--save', type=str, help='Where to save the calibrator model after training')


def main(args):
    if path.isfile(args.confidence_path):
        # load from cache
        with open(args.confidence_path, 'rb') as f:
            confidences = pickle.load(f)
    else:
        exit(1)

    for f, name in [
                    ([logit_mean_0], 'mean'),
                    ([nodrop_entropies_0], 'entropy'), 
                    # ([logit_mean_0, nodrop_entropies_0], 'mean+entropy'),
                    # ([(logit_mean_0, nodrop_entropies_0)], 'mean+entropy interleave'),
                    # ([length_0, logit_mean_0, nodrop_entropies_0], 'length+mean+entropy'),
                    # ([length_0, nodroplogit_0, nodrop_entropies_0], 'length+nodroplog+entropy'),
                    ]:
        print('name = ', name)
        precision, recall, pass_rate, accuracies, thresholds = run(confidences, f)
        pyplot.figure(0)
        pyplot.plot(recall, precision, marker='.', label=name)
        # print('recall = ', recall)
        # print('precision = ', precision)
        # pyplot.plot(recall, 2*(precision*recall)/(precision+recall), marker='.', label=name+' (F1)')
        pyplot.figure(1)
        # print('thresholds = ', thresholds)
        # print('-'*10)
        pyplot.plot(range(len(thresholds)), thresholds, marker='*', label=name+ ' (thresholds)')
        pyplot.figure(2)
        pyplot.plot(pass_rate, accuracies, marker='.', label=name)
        

    avg_logprobs = [avg_logprob(c) for c in confidences]
    all_labels = []
    for c in confidences:
        all_labels.append(c[0].first_mistake)
    all_labels = np.array(all_labels) + 1 # +1 so that minimum is 0
    all_labels = (all_labels == 0)

    all_labels_train, all_labels_dev, avg_logprobs_train, avg_logprobs_dev = \
        sklearn.model_selection.train_test_split(all_labels, avg_logprobs, test_size=0.2, random_state=123)

    logit_precision, logit_recall, thresholds = precision_recall_curve(all_labels_dev, avg_logprobs_dev)
    pyplot.figure(0)
    pyplot.plot(logit_recall, logit_precision, marker='.', label='average logprob')
    # thresholds = list(thresholds)+[1.0]
    # pyplot.plot(logit_recall, thresholds, marker='*', label='average logprob (threshold)')
    pyplot.legend()
    pyplot.grid()
    pyplot.xticks(np.arange(0, 1, 0.1))
    pyplot.xlim(0, 1)
    pyplot.xlabel('Recall')
    pyplot.ylabel('Precision')
    pyplot.savefig('precision-recall.png')

    pyplot.figure(1)
    pyplot.legend()
    pyplot.grid()
    pyplot.xlabel('Index')
    pyplot.ylabel('Confidence Threshold')
    pyplot.savefig('threshold.png')

    pass_rates, accuracies = accuracy_at_pass_rate(all_labels_dev, avg_logprobs_dev)
    pyplot.figure(2)
    pyplot.plot(pass_rates, accuracies, marker='.', label='average logprob')
    pyplot.legend()
    pyplot.grid()
    pyplot.xticks(np.arange(0, 1, 0.1))
    pyplot.xlim(0, 1)
    pyplot.xlabel('Pass Rate')
    pyplot.ylabel('Accuracy')
    pyplot.savefig('pass-accuracy.png')
    