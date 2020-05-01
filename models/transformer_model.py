import math
import numpy as np
import torch
import torch.nn as nn
from models.tpc_model import MSLELoss, MSELoss, MyBatchNorm1d, EmptyModule
from torch import exp, cat

# PositionalEncoding adapted from https://pytorch.org/tutorials/beginner/transformer_tutorial.html. I made the following
# changes:
    # Took out the dropout
    # Changed the dimensions/shape of pe
# I am using the positional encodings suggested by Vaswani et al. as the Attend and Diagnose authors do not specify in
# detail how they do their positional encodings.
class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=14*24):
        super(PositionalEncoding, self).__init__()

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).permute(0, 2, 1)  # changed from max_len * d_model to 1 * d_model * max_len
        self.register_buffer('pe', pe)

    def forward(self, X):
        # X is B * d_model * T
        # self.pe[:, :, :X.size(2)] is 1 * d_model * T but is broadcast to B when added
        X = X + self.pe[:, :, :X.size(2)]  # B * d_model * T
        return X  # B * d_model * T


class TransformerEncoder(nn.Module):
    def __init__(self, input_size=None, d_model=128, num_layers=4, num_heads=8, feedforward_size=32, dropout=0.3):
        super(TransformerEncoder, self).__init__()

        self.d_model = d_model
        self.input_embedding = nn.Conv1d(in_channels=input_size, out_channels=d_model, kernel_size=1)  # B * C * T
        self.pos_encoder = PositionalEncoding(d_model)
        self.trans_encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=num_heads,
                                                              dim_feedforward=feedforward_size, dropout=dropout,
                                                              activation='relu')
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer=self.trans_encoder_layer, num_layers=num_layers)

    def _causal_mask(self, size=None):
        mask = (torch.triu(torch.ones(size, size)) == 1).transpose(0, 1)
        mask = mask.float().masked_fill(mask == 0, float('-inf')).masked_fill(mask == 1, float(0.0))
        return mask  # T * T

    def forward(self, X, T):
        # X is B * (2F + 2) * T

        # multiplication by root(d_model) as described in Vaswani et al. 2017 section 3.4
        X = self.input_embedding(X) * math.sqrt(self.d_model)  # B * d_model * T
        X = self.pos_encoder(X)  # B * d_model * T
        X = self.transformer_encoder(src=X.permute(2, 0, 1), mask=self._causal_mask(size=T))  # T * B * d_model
        return X.permute(1, 2, 0)  # B * d_model * T


class Transformer(nn.Module):
    def __init__(self, config, F=None, D=None, no_flat_features=None):

        # The timeseries data will be of dimensions B * (2F + 2) * T where:
        #   B is the batch size
        #   F is the number of features for convolution (N.B. we start with 2F because there are corresponding mask features)
        #   T is the number of timepoints
        #   The other 2 features represent the sequence number and the hour in the day

        # The diagnoses data will be of dimensions B * D where:
        #   D is the number of diagnoses
        # The flat data will be of dimensions B * no_flat_features

        super(Transformer, self).__init__()
        self.d_model = config.d_model
        self.n_layers = config.n_layers
        self.n_heads = config.n_heads
        self.feedforward_size = config.feedforward_size
        self.trans_dropout_rate = config.trans_dropout_rate
        self.main_dropout_rate = config.main_dropout_rate
        self.diagnosis_size = config.diagnosis_size
        self.batchnorm = config.batchnorm
        self.last_linear_size = config.last_linear_size
        self.n_layers = config.n_layers
        self.F = F
        self.D = D
        self.no_flat_features = no_flat_features
        self.no_exp = config.no_exp
        self.momentum = 0.01 if self.batchnorm == 'low_momentum' else 0.1

        self.relu = nn.ReLU()
        self.hardtanh = nn.Hardtanh(min_val=1 / 48, max_val=100)  # keep the end predictions between half an hour and 100 days
        self.trans_dropout = nn.Dropout(p=self.trans_dropout_rate)
        self.main_dropout = nn.Dropout(p=self.main_dropout_rate)
        self.msle_loss = MSLELoss()
        self.mse_loss = MSELoss()

        self.empty_module = EmptyModule()
        self.remove_none = lambda x: tuple(xi for xi in x if xi is not None)

        # note if it's bidirectional, then we can't assume there's no influence from future timepoints on past ones
        self.transformer = TransformerEncoder(input_size=(2*self.F + 2), d_model=self.d_model, num_layers=self.n_layers,
                                              num_heads=self.n_heads, feedforward_size=self.feedforward_size,
                                              dropout=self.trans_dropout_rate)

        # input shape: B * D
        # output shape: B * diagnosis_size
        self.diagnosis_encoder = nn.Linear(in_features=self.D, out_features=self.diagnosis_size)

        # input shape: B * diagnosis_size
        if self.batchnorm in ['mybatchnorm', 'low_momentum']:
            self.bn_diagnosis_encoder = MyBatchNorm1d(num_features=self.diagnosis_size, momentum=self.momentum)
        elif self.batchnorm == 'default':
            self.bn_diagnosis_encoder = nn.BatchNorm1d(num_features=self.diagnosis_size)
        else:
            self.bn_diagnosis_encoder = self.empty_module

        # input shape: (B * T) * (d_model + diagnosis_size + no_flat_features)
        # output shape: (B * T) * last_linear_size
        self.point = nn.Linear(in_features=self.d_model + self.diagnosis_size + self.no_flat_features,
                                 out_features=self.last_linear_size)

        # input shape: (B * T) * last_linear_size
        if self.batchnorm in ['mybatchnorm', 'pointonly', 'low_momentum']:
            self.bn_point_last = MyBatchNorm1d(num_features=self.last_linear_size, momentum=self.momentum)
        elif self.batchnorm == 'default':
            self.bn_point_last = nn.BatchNorm1d(num_features=self.last_linear_size)
        else:
            self.bn_point_last = self.empty_module

        # input shape: (B * T) * last_linear_size
        # output shape: (B * T) * 1
        self.point_final = nn.Linear(in_features=self.last_linear_size, out_features=1)

        return

    def forward(self, X, diagnoses, flat):

        # flat is B * no_flat_features
        # diagnoses is B * D
        # X is B * (2F + 2) * T
        # X_mask is B * T
        # (the batch is padded to the longest sequence)

        B, _, T = X.shape  # B * (2F + 2) * T

        trans_output = self.transformer(X, T)  # B * d_model * T

        X_final = self.relu(self.trans_dropout(trans_output))  # B * d_model * T

        diagnoses_enc = self.relu(self.main_dropout(self.bn_diagnosis_encoder(self.diagnosis_encoder(diagnoses))))  # B * diagnosis_size

        # note that we cut off at 5 hours here because the model is only valid from 5 hours onwards
        combined_features = cat((flat.repeat_interleave(T - 5, dim=0),  # (B * (T - 5)) * no_flat_features
                                 diagnoses_enc.repeat_interleave(T - 5, dim=0),  # (B * (T - 5)) * diagnosis_size
                                 X_final[:, :, 5:].permute(0, 2, 1).contiguous().view(B * (T - 5), -1)), dim=1)

        last_point = self.relu(self.main_dropout(self.bn_point_last(self.point(combined_features))))

        if self.no_exp:
            predictions = self.hardtanh(self.point_final(last_point).view(B, T - 5))  # B * (T - 5)
        else:
            predictions = self.hardtanh(exp(self.point_final(last_point).view(B, T - 5)))  # B * (T - 5)

        return predictions

    def loss(self, y_hat, y, mask, seq_lengths, device, sum_losses, loss_type):
        bool_type = torch.cuda.BoolTensor if device == torch.device('cuda') else torch.BoolTensor
        if loss_type == 'msle':
            loss = self.msle_loss(y_hat, y, mask.type(bool_type), seq_lengths, sum_losses)
        elif loss_type == 'mse':
            loss = self.mse_loss(y_hat, y, mask.type(bool_type), seq_lengths, sum_losses)
        return loss