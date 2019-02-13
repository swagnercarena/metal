python launch.py --device 0 --bert_model bert-base-uncased --bert_output_dim 768 --max_len 200 --batch_size 32 --lr 1e-5 --l2 0.01 --lr_scheduler exponential --log_every 0.25 --score_every 0.5 --checkpoint_dir test_logs --checkpoint_metric train/loss --checkpoint_metric_mode min --tasks COLA,SST2,MNLI,RTE,WNLI,QQP,MRPC,STSB,QNLI --n_epochs 5 --max_datapoints 1000 --run_dir test_mtl --run_name v2 --score_every 0.1 --log_every 0.1
