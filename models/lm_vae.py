import math
import time

import numpy
import torch
import torch.nn as nn
import torch.optim as optim
import pytorch_lightning as pl

from utils.lagging_encoder import *
from utils.vae_loss import *

class LSTM_Encoder(nn.Module):

    def __init__(self, vocab, embedding_dims, hidden_dims, latent_dims):
        super(LSTM_Encoder, self).__init__()

        self.latent_dims = latent_dims
        self.vocab = vocab
        self.hidden_dims = hidden_dims
        self.embedding_dims = embedding_dims

        padding_idx = vocab['<pad>']
        self.embed = nn.Embedding(len(vocab), self.embedding_dims,
                                  padding_idx=padding_idx)

        self.lstm = nn.LSTM(input_size=self.embedding_dims,
                            hidden_size=self.hidden_dims,
                            num_layers=1,
                            dropout=0,
                            batch_first=False)

        self.stats = nn.Linear(in_features=self.hidden_dims, out_features=2 * self.latent_dims)

        self.reset_parameters(std=0.01)

    @property
    def device(self):
        return next(self.parameters()).device

    def reset_parameters(self, std=0.01):
        for param in self.parameters():
            nn.init.uniform_(param, -std, std)
        nn.init.uniform_(self.embed.weight, -std * 10, std * 10)

    def forward(self, input):

        # (batch_size, seq_len-1, args.ni)
        embedding = self.embed(input)

        _, (h_T, c_T) = self.lstm(embedding)

        mean, log_std = self.stats(h_T).chunk(2, -1)

        return mean, log_std

    @torch.no_grad()
    def MutualInformation(self, x, stats=None, z_iters=1, debug=False):
        """
        Approximate the mutual information between x and z,
        I(x, z) = E_xE_{q(z|x)}log(q(z|x)) - E_xE_{q(z|x)}log(q(z)).
        Adapted from https://github.com/jxhe/vae-lagging-encoder/blob/master/modules/encoders/encoder.py
        Returns: Float
        """

        if stats == None:
            mean, logstd = self.forward(x)
        else:
            mean, logstd = stats

        logvar = 2 * logstd

        x_batch, z_dim = mean.shape[1], mean.shape[2]
        z_batch = x_batch * z_iters

        # E_{q(z|x)}log(q(z|x)) = -0.5*(K+L)*log(2*\pi) - 0.5*(1+logvar).sum(-1)
        neg_entropy = (-0.5 * z_dim * math.log(2 * math.pi) - 0.5 * (1 + logvar).sum(-1)).mean()

        # [z_batch, 1, z_dim]
        z_samples = []
        for i in range(z_iters):
            z_samples.append(sample_reparameterize(mean, logstd).permute(1, 0, 2))
        z_samples = torch.cat(z_samples, dim=0)
        if debug:
            print('[z_batch, 1, z_dim]', z_samples.shape)

        # [1, x_batch, z_dim]
        var = logvar.exp()
        if debug:
            print('[1, x_batch, z_dim]', mean.shape)

        # (z_batch, x_batch)
        log_density = -0.5 * (((z_samples - mean) ** 2) / var).sum(dim=-1) - \
            0.5 * (z_dim * math.log(2 * math.pi) + logvar.sum(-1))
        if debug:
            print('(z_batch, x_batch)', log_density.shape)

        # log q(z): aggregate posterior
        # [z_batch]
        log_qz = log_sum_exp(log_density, dim=1) - math.log(x_batch)
        if debug:
            print('[z_batch]', log_qz.shape)

        return (neg_entropy - log_qz.mean(-1)).detach()

class LSTM_Decoder(nn.Module):

    def __init__(self, vocab, embedding_dims, hidden_dims, latent_dims):
        super(LSTM_Decoder, self).__init__()

        self.latent_dims = latent_dims
        self.vocab = vocab
        self.embedding_dims = embedding_dims
        self.hidden_dims = hidden_dims

        self.linear_in = nn.Linear(self.latent_dims, self.hidden_dims)

        padding_idx = vocab['<pad>']
        self.embed = nn.Embedding(len(vocab), embedding_dims,
                                  padding_idx=padding_idx)

        self.dropout_in = nn.Dropout(p=0.5)

        self.lstm = nn.LSTM(input_size=self.embedding_dims + self.latent_dims,
                            hidden_size=self.hidden_dims,
                            num_layers=1,
                            dropout=0,
                            batch_first=False)

        self.dropout_out = nn.Dropout(p=0.5)
        self.linear_out = nn.Linear(self.hidden_dims, len(vocab))

        self.reset_parameters(std=0.01)

    @property
    def device(self):
        return next(self.parameters()).device

    def reset_parameters(self, std=0.01):
        for param in self.parameters():
            nn.init.uniform_(param, -std, std)
        nn.init.uniform_(self.embed.weight, -std * 10, std * 10)

    def forward(self, z, input=None, debug=False):
        """
        Args:
            input: (seq_len, batch_size)
            z: (1, batch_size, nz)
        """

        batch_size, z_dim = z.size(-2), z.size(-1)

        # (seq_len, batch_size, embedding_dim)
        if input != None:

            seq_len = input.size(0)

            word_embed = self.embed(input)
            if debug:
                print('(seq_len, batch_size, embedding_dim)', word_embed.shape)

            word_embed = self.dropout_in(word_embed)

            z_ = z.expand(seq_len, batch_size, z_dim)

            # (seq_len, batch_size, embedding_dim + z_dim)
            word_embed = torch.cat((word_embed, z_), dim=-1)
            if debug:
                print('(seq_len, batch_size, embedding_dim + z_dim)', word_embed.shape)

            z = z.view(batch_size, z_dim)
            c_init = self.linear_in(z).unsqueeze(0)
            h_init = torch.tanh(c_init)

            output, _ = self.lstm(word_embed, (h_init, c_init))

            output = self.dropout_out(output)
            if debug:
                print('(seq_len, batch_size, vocab_size)', output.shape)
            output_logits = self.linear_out(output)

        else:

            seq_len = 81

            input = torch.full(size=(1, z.size(0)),
                               fill_value=self.vocab['<s>'],
                               dtype=torch.long, device=self.device)

            z = z.view(batch_size, z_dim)
            c = self.linear_in(z).unsqueeze(0)
            h = torch.tanh(c)

            output_logits = []
            for t in range(seq_len):
                # (1, batch_size, embedding_dim)
                word_embed = self.embed(input)
                word_embed = self.dropout_in(word_embed)

                # (1, batch_size, embedding_dim + hidden_dim)
                word_embed = torch.cat((word_embed, z.unsqueeze(0)), dim=-1)

                # (1, batch_size, hidden_dim)
                output, (h, c) = self.lstm(word_embed, (h, c))
                output = self.dropout_out(output)

                # (1, batch_size, vocab_size)
                logits_t = self.linear_out(output)

                # (1, batch_size)
                input = torch.argmax(logits_t, dim=-1)

                output_logits.append(logits_t)

            # (seq_len, batch_size, vocab_size)
            output_logits = torch.cat(output_logits, dim=0)

        return output_logits

    @torch.no_grad()
    def beam_search_decode(self, z, K=5, max_length=30):
        """beam search decoding, code is based on
        https://github.com/pcyin/pytorch_basic_nmt/blob/master/nmt.py
        the current implementation decodes sentence one by one, further batching would improve the speed
        Args:
            z: (batch_size, nz)
            K: the beam width
        Returns: List1
            List1: the decoded word sentence list
        """

        batch_size, nz = z.size()
        decoded_batch = []  # [[] for _ in range(batch_size)]

        # (1, batch_size, nz)
        c_init = self.linear_in(z).unsqueeze(0)
        h_init = torch.tanh(c_init)

        # decoding goes sentence by sentence
        for idx in range(batch_size):
            # Start with the start of the sentence token
            decoder_input = torch.tensor([[self.vocab["<s>"]]], dtype=torch.long, device=self.device)
            decoder_hidden = (h_init[:, idx, :].unsqueeze(0), c_init[:, idx, :].unsqueeze(0))

            node = BeamSearchNode(decoder_hidden, None, decoder_input, 0., 1)
            live_hypotheses = [node]

            completed_hypotheses = []

            t = 0
            while len(completed_hypotheses) < K and t < max_length:
                t += 1

                # (1, len(live))
                decoder_input = torch.cat([node.wordid for node in live_hypotheses], dim=1)

                # (1, len(live), nh)
                decoder_hidden_h = torch.cat([node.h[0] for node in live_hypotheses], dim=1)
                decoder_hidden_c = torch.cat([node.h[1] for node in live_hypotheses], dim=1)

                decoder_hidden = (decoder_hidden_h, decoder_hidden_c)

                # (1, len(live), ni) --> (1, len(live), ni+nz)
                word_embed = self.embed(decoder_input)
                word_embed = torch.cat((word_embed, z[idx].view(1, 1, -1).expand(
                    1, len(live_hypotheses), nz)), dim=-1)

                output, decoder_hidden = self.lstm(word_embed, decoder_hidden)

                # (1, len(live), vocab_size)
                output_logits = self.linear_out(output)
                decoder_output = F.log_softmax(output_logits, dim=-1)

                prev_logp = torch.tensor([node.logp for node in live_hypotheses], dtype=torch.float, device=self.device)
                decoder_output = decoder_output + prev_logp.view(1, len(live_hypotheses), 1)

                # (len(live) * vocab_size)
                decoder_output = decoder_output.view(-1)

                # (K)
                log_prob, indexes = torch.topk(decoder_output, K - len(completed_hypotheses))

                live_ids = indexes // len(self.vocab)
                word_ids = indexes % len(self.vocab)

                live_hypotheses_new = []
                for live_id, word_id, log_prob_ in zip(live_ids, word_ids, log_prob):
                    node = BeamSearchNode((decoder_hidden[0][:, live_id, :].unsqueeze(1),
                                           decoder_hidden[1][:, live_id, :].unsqueeze(1)),
                                          live_hypotheses[live_id], word_id.view(1, 1), log_prob_, t)

                    if word_id.item() == self.vocab["</s>"]:
                        completed_hypotheses.append(node)
                    else:
                        live_hypotheses_new.append(node)

                live_hypotheses = live_hypotheses_new

                if len(completed_hypotheses) == K:
                    break

            for live in live_hypotheses:
                completed_hypotheses.append(live)

            utterances = []
            for n in sorted(completed_hypotheses, key=lambda node: node.logp, reverse=True):
                utterance = []
                utterance.append(self.vocab.itos[n.wordid.item()])
                # back trace
                while n.prevNode != None:
                    n = n.prevNode
                    token = self.vocab.itos[n.wordid.item()]
                    if token != '<s>':
                        utterance.append(self.vocab.itos[n.wordid.item()])
                    else:
                        break

                utterance = utterance[::-1]

                utterances.append(utterance)

                # only save the top 1
                break

            decoded_batch.append(utterances[0])

        decoded_batch = [' '.join(sent) for sent in decoded_batch]

        return decoded_batch

    @torch.no_grad()
    def greedy_decode(self, z, max_length=30):
        """greedy decoding from z
        Args:
            z: (batch_size, nz)
        Returns: List1
            List1: the decoded word sentence list
        """

        batch_size, nz = z.size()
        decoded_batch = [[] for _ in range(batch_size)]

        # (1, batch_size, nz)
        c_init = self.linear_in(z).unsqueeze(0)
        h_init = torch.tanh(c_init)

        decoder_hidden = (h_init, c_init)
        decoder_input = torch.tensor([self.vocab["<s>"]] * batch_size,
                                     dtype=torch.long, device=self.device).unsqueeze(0)
        end_symbol = torch.tensor([self.vocab["</s>"]] * batch_size, dtype=torch.long, device=self.device)

        mask = torch.ones((batch_size), dtype=torch.uint8, device=self.device)
        length_c = 1
        while mask.sum().item() != 0 and length_c < max_length:

            # (batch_size, 1, ni) --> (batch_size, 1, ni+nz)
            word_embed = self.embed(decoder_input)
            word_embed = word_embed.squeeze(2)
            word_embed = torch.cat((word_embed, z.unsqueeze(0)), dim=-1)

            output, decoder_hidden = self.lstm(word_embed, decoder_hidden)

            # (batch_size, 1, vocab_size) --> (batch_size, vocab_size)
            decoder_output = self.linear_out(output)
            output_logits = decoder_output.squeeze(0)

            # (batch_size)
            max_index = torch.argmax(output_logits, dim=1)

            decoder_input = max_index.unsqueeze(0)
            length_c += 1

            for i in range(batch_size):
                if mask[i].item():
                    decoded_batch[i].append(self.vocab.itos[max_index[i].item()])

            mask = (max_index != end_symbol) * mask

        decoded_batch = [' '.join(sent) for sent in decoded_batch]

        return decoded_batch

    @torch.no_grad()
    def sample_decode(self, z, max_length=30):
        """sampling decoding from z
        Args:
            z: (batch_size, nz)
        Returns: List1
            List1: the decoded word sentence list
        """

        batch_size, nz = z.size()
        decoded_batch = [[] for _ in range(batch_size)]

        # (1, batch_size, nz)
        c_init = self.linear_in(z).unsqueeze(0)
        h_init = torch.tanh(c_init)

        decoder_hidden = (h_init, c_init)
        decoder_input = torch.tensor([self.vocab["<s>"]] * batch_size,
                                     dtype=torch.long, device=self.device).unsqueeze(0)
        end_symbol = torch.tensor([self.vocab["</s>"]] * batch_size, dtype=torch.long, device=self.device)

        mask = torch.ones((batch_size), dtype=torch.uint8, device=self.device)
        length_c = 1
        while mask.sum().item() != 0 and length_c < max_length:

            # (batch_size, 1, ni) --> (batch_size, 1, ni+nz)
            word_embed = self.embed(decoder_input)
            word_embed = word_embed.squeeze(2)
            word_embed = torch.cat((word_embed, z.unsqueeze(0)), dim=-1)

            output, decoder_hidden = self.lstm(word_embed, decoder_hidden)

            # (batch_size, 1, vocab_size) --> (batch_size, vocab_size)
            decoder_output = self.linear_out(output)
            output_logits = decoder_output.squeeze(0)

            # (batch_size)
            sample_prob = F.softmax(output_logits, dim=1)
            sample_index = torch.multinomial(sample_prob, num_samples=1).squeeze()

            decoder_input = sample_index.unsqueeze(0)
            length_c += 1

            for i in range(batch_size):
                if mask[i].item():
                    decoded_batch[i].append(self.vocab.itos[sample_index[i].item()])

            mask = (sample_index != end_symbol) * mask

        decoded_batch = [' '.join(sent) for sent in decoded_batch]

        return decoded_batch

class lm_VAE(pl.LightningModule):

    def __init__(self, vocab, embedding_dims, hidden_dims, latent_dims, z_iters, aggressive, inner_iter, kl_weight_start, anneal_rate, decoding_strategy, max_aggressive_epochs, min_scheduler_epoch, aggressive_patience):

        super().__init__()
        self.save_hyperparameters()

        self.vocab = vocab
        self.embedding_dims = embedding_dims
        self.hidden_dims = hidden_dims
        self.latent_dims = latent_dims
        self.z_iters = z_iters
        self.anneal_rate = anneal_rate
        self.kl_weight = kl_weight_start
        self.aggressive = aggressive
        self.inner_iter = inner_iter
        self.decoding_strategy = decoding_strategy
        self.max_aggressive_epochs = max_aggressive_epochs
        self.min_scheduler_epoch = min_scheduler_epoch

        self.mi_patience = aggressive_patience
        self.stable_mi = False
        self.mi_prev = 0
        self.mi_curr = 0
        self.val_batches = 1

        self.encoder = LSTM_Encoder(vocab=self.vocab,
                                    embedding_dims=self.embedding_dims,
                                    hidden_dims=hidden_dims,
                                    latent_dims=self.latent_dims)

        self.decoder = LSTM_Decoder(vocab=self.vocab,
                                    embedding_dims=self.embedding_dims,
                                    hidden_dims=self.hidden_dims,
                                    latent_dims=self.latent_dims)

        vocab_mask = torch.ones(len(self.vocab))
        self.loss_fn = nn.CrossEntropyLoss(weight=vocab_mask, reduction='none', ignore_index=self.vocab['<pad>'])

        self.t0 = time.time()

    def forward(self, batch, calc_mi=False):

        text, _ = batch.text, batch.label

        mean, log_std = self.encoder(text)

        L_reg = KLD(mean, log_std)

        z = sample_reparameterize(mean, log_std)

        target = text[1:]
        logits = self.decoder.forward(z, text[:-1])

        L_rec = self.loss_fn(logits.view(-1, logits.size(-1)),
                             target.view(-1))
        L_rec = torch.sum(L_rec.view(logits.size(1), -1), dim=-1)

        if calc_mi:
            mi = self.encoder.MutualInformation(text,
                                                stats=(mean, log_std),
                                                z_iters=self.z_iters)
            return torch.mean(L_rec), torch.mean(L_reg), mi

        return torch.mean(L_rec), torch.mean(L_reg)

    def configure_optimizers(self):
        encoder_opt = optim.SGD(self.encoder.parameters(), lr=1.0, momentum=0)
        decoder_opt = optim.SGD(self.decoder.parameters(), lr=1.0, momentum=0)

        enc_sched = optim.lr_scheduler.ReduceLROnPlateau(encoder_opt, factor=0.5, patience=1,
                                                         verbose=True)
        enc_sched_dict = {
            'scheduler': enc_sched,
            'interval': 'epoch',
            'frequency': 1,
            'reduce_on_plateau': True,
            'monitor': 'Valid ELBO',
            'strict': True
        }

        dec_sched = optim.lr_scheduler.ReduceLROnPlateau(decoder_opt, factor=0.5, patience=1,
                                                         verbose=True)
        dec_sched_dict = {
            'scheduler': dec_sched,
            'interval': 'epoch',
            'frequency': 1,
            'reduce_on_plateau': True,
            'monitor': 'Valid ELBO',
            'strict': True
        }

        return [encoder_opt, decoder_opt], [enc_sched_dict, dec_sched_dict]

    def training_step(self, batch, batch_idx, optimizer_idx):

        if batch_idx == 0:
            self.mi_curr = self.mi_curr / self.val_batches
            if (not self.aggressive) or self.mi_curr < self.mi_prev:
                if (not self.stable_mi):
                    self.stable_mi = True
                self.mi_patience -= 1
                print('MI has not improved. Will stop aggressive training in', self.mi_patience, 'epochs.')
            if self.mi_patience <= 0:
                self.aggressive = False

            self.mi_prev = self.mi_curr
            self.mi_curr, self.val_batches = 0, 0

        if self.current_epoch > self.max_aggressive_epochs-1:
            self.aggressive = False

        if self.global_step == 0:
            for schedule_dict in self.trainer.lr_schedulers:
                schedule_dict['scheduler'].cooldown_counter = self.min_scheduler_epoch-1

        (encoder_opt, decoder_opt) = self.optimizers()

        i=0
        if self.aggressive:

            burn_num_words = 0
            burn_pre_loss = 1e4
            burn_cur_loss = 0
            for i in range(1, self.inner_iter+1):

                L_rec, L_reg = self.forward(batch)

                loss = L_rec + self.kl_weight * L_reg

                self.manual_backward(loss, encoder_opt)
                encoder_opt.step()

                self.log("Train - Inner ELBO", L_rec + L_reg)

                burn_sents_len, burn_batch_size = batch.text.size()
                burn_num_words += (burn_sents_len - 1) * burn_batch_size
                burn_cur_loss += loss.sum().detach()

                if i % (self.inner_iter//10) == 0:
                    burn_cur_loss = burn_cur_loss / burn_num_words
                    if burn_pre_loss - burn_cur_loss < 0:
                        self.log("Train - Inner Steps", i)
                        break
                    burn_pre_loss = burn_cur_loss
                    burn_cur_loss = burn_num_words = 0

        L_rec, L_reg = self.forward(batch)

        loss = L_rec + self.kl_weight * L_reg

        self.log("Train - Outer L_rec", L_rec,
                 on_step=True, on_epoch=False)
        self.log("Train - Outer L_reg", L_reg,
                 on_step=True, on_epoch=False)
        self.log("Train - Outer ELBO", L_rec + L_reg)
        self.log("Train - KLD Weight", self.kl_weight,
                 on_step=True, on_epoch=False)

        if not self.aggressive:
            self.manual_backward(loss, encoder_opt, retain_graph=True)
        self.manual_backward(loss, decoder_opt)

        if not self.aggressive:
            encoder_opt.step()
        decoder_opt.step()

        self.kl_weight = min(1.0, self.kl_weight + self.anneal_rate)

        if batch_idx == 0 or batch_idx % 5 == 0:
            t1 = time.time()
            dt = (t1 - self.t0) / 60
            mins, secs = int(dt), int((dt - int(dt)) * 60)

            print(f"Time: {mins:4d}m {secs:2d}s| Train {int(self.current_epoch):03d}.{int(batch_idx):03d}: L_rec={L_rec:6.2f}, L_reg={L_reg:6.2f}, Inner iters={int(i):02d}, KL weight={self.kl_weight:4.2f}")

        return loss

    def validation_step(self, batch, batch_idx):

        L_rec, L_reg, mi = self.forward(batch, calc_mi=True)

        self.log("Valid Reconstruction Loss", L_rec,
                 on_epoch=True)
        self.log("Valid Regularization Loss", L_reg,
                 on_epoch=True)
        self.log("Valid Encoder MI", mi,
                 on_epoch=True)

        loss = L_rec + self.kl_weight * L_reg

        self.log("Valid ELBO", L_rec + L_reg)

        t1 = time.time()
        dt = (t1 - self.t0) / 60
        mins, secs = int(dt), int((dt - int(dt)) * 60)

        print(f"Time: {mins:4d}m {secs:2d}s| Valid {int(self.current_epoch):03d}.{int(batch_idx):03d}: L_rec={L_rec:6.2f}, L_reg={L_reg:6.2f}, MI={mi:5.2f}")

        self.mi_curr += mi
        self.val_batches += 1

        return loss

    def test_step(self, batch, batch_idx):

        L_rec, L_reg, mi = self.forward(batch, calc_mi=True)

        self.log("Test Reconstruction Loss", L_rec)
        self.log("Test Regularization Loss", L_reg)
        self.log("Test Encoder MI", mi)
        self.log("Test ELBO", L_rec + L_reg)

    @torch.no_grad()
    def decode(self, text, decoding_strategy=None, beam_length=5):

        mean, log_std = self.encoder(text)
        z = sample_reparameterize(mean, log_std)
        z = z.squeeze()

        if decoding_strategy == None:
            decoding_strategy = self.decoding_strategy

        if decoding_strategy == 'beam_search':
            text_sample = self.decoder.beam_search_decode(z, beam_length)
        elif decoding_strategy == 'greedy':
            text_sample = self.decoder.greedy_decode(z)
        elif decoding_strategy == 'sample':
            text_sample = self.decoder.sample_decode(z)

        return text_sample

    @torch.no_grad()
    def latent_sweep(self, text, zi, num=7, decoding_strategy=None, beam_length=5, tau=0.5):

        if decoding_strategy == None:
            decoding_strategy = self.decoding_strategy

        if len(text.size()) >= 1:
            input = text[:, 0]
        else:
            input = text

        mean, log_std = self.encoder(input.unsqueeze(1))
        z_init = sample_reparameterize(mean, log_std).squeeze()

        vals = np.linspace(-3, 3, num=num)
        sweep = []
        for val in vals:
            z_adj = z_init.clone()
            z_adj[zi] += val

            sweep.append(z_adj)

        sweep = torch.stack(sweep, dim=0)

        if decoding_strategy == 'beam_search':
            text_sample = self.decoder.beam_search_decode(sweep, beam_length)
        elif decoding_strategy == 'greedy':
            text_sample = self.decoder.greedy_decode(sweep)
        elif decoding_strategy == 'sample':
            text_sample = self.decoder.sample_decode(sweep)

        sweep_text = []
        for n, sent in enumerate(text_sample):
            sweep_text.append(['%+4.2f' % (vals[n]), sent])

        return sweep_text
